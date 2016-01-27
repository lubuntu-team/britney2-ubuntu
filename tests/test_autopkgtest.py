#!/usr/bin/python3
# (C) 2014 - 2015 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import sys
import fileinput
import unittest
import json
import pprint
import urllib.parse

import apt_pkg
import yaml

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from tests import TestBase, mock_swift

apt_pkg.init()


# shortcut for test triggers
def tr(s):
    return {'custom_environment': ['ADT_TEST_TRIGGERS=%s' % s]}


class T(TestBase):
    '''AMQP/cloud interface'''

    ################################################################
    # Common test code
    ################################################################

    def setUp(self):
        super().setUp()
        self.fake_amqp = os.path.join(self.data.path, 'amqp')

        # Set fake AMQP and Swift server
        for line in fileinput.input(self.britney_conf, inplace=True):
            if 'ADT_AMQP' in line:
                print('ADT_AMQP = file://%s' % self.fake_amqp)
            elif 'ADT_SWIFT_URL' in line:
                print('ADT_SWIFT_URL = http://localhost:18085')
            elif 'ADT_ARCHES' in line:
                print('ADT_ARCHES = amd64 i386')
            else:
                sys.stdout.write(line)

        # add a bunch of packages to testing to avoid repetition
        self.data.add('libc6', False)
        self.data.add('libgreen1', False, {'Source': 'green',
                                           'Depends': 'libc6 (>= 0.9)'},
                      testsuite='autopkgtest')
        self.data.add('green', False, {'Depends': 'libc6 (>= 0.9), libgreen1',
                                       'Conflicts': 'blue'},
                      testsuite='autopkgtest')
        self.data.add('lightgreen', False, {'Depends': 'libgreen1'},
                      testsuite='autopkgtest')
        # autodep8 or similar test
        self.data.add('darkgreen', False, {'Depends': 'libgreen1'},
                      testsuite='autopkgtest-pkg-foo')
        self.data.add('blue', False, {'Depends': 'libc6 (>= 0.9)',
                                      'Conflicts': 'green'},
                      testsuite='specialtest')

        # create mock Swift server (but don't start it yet, as tests first need
        # to poke in results)
        self.swift = mock_swift.AutoPkgTestSwiftServer(port=18085)
        self.swift.set_results({})

    def tearDown(self):
        del self.swift

    def do_test(self, unstable_add, expect_status, expect_excuses={}):
        '''Run britney with some unstable packages and verify excuses.

        unstable_add is a list of (binpkgname, field_dict, testsuite_value)
        passed to TestData.add for "unstable".

        expect_status is a dict sourcename → (is_candidate, testsrc → arch → status)
        that is checked against the excuses YAML.

        expect_excuses is a dict sourcename →  [(key, value), ...]
        matches that are checked against the excuses YAML.

        Return (output, excuses_dict).
        '''
        for (pkg, fields, testsuite) in unstable_add:
            self.data.add(pkg, True, fields, True, testsuite)

        self.swift.start()
        (excuses_yaml, excuses_html, out) = self.run_britney()
        self.swift.stop()

        # convert excuses to source indexed dict
        excuses_dict = {}
        for s in yaml.load(excuses_yaml)['sources']:
            excuses_dict[s['source']] = s

        if 'SHOW_EXCUSES' in os.environ:
            print('------- excuses -----')
            pprint.pprint(excuses_dict, width=200)
        if 'SHOW_HTML' in os.environ:
            print('------- excuses.html -----\n%s\n' % excuses_html)
        if 'SHOW_OUTPUT' in os.environ:
            print('------- output -----\n%s\n' % out)

        for src, (is_candidate, testmap) in expect_status.items():
            self.assertEqual(excuses_dict[src]['is-candidate'], is_candidate,
                             src + ': ' + pprint.pformat(excuses_dict[src]))
            for testsrc, archmap in testmap.items():
                for arch, status in archmap.items():
                    self.assertEqual(excuses_dict[src]['tests']['autopkgtest'][testsrc][arch][0],
                                     status,
                                     excuses_dict[src]['tests']['autopkgtest'][testsrc])

        for src, matches in expect_excuses.items():
            for k, v in matches:
                if isinstance(excuses_dict[src][k], list):
                    self.assertIn(v, excuses_dict[src][k])
                else:
                    self.assertEqual(excuses_dict[src][k], v)

        self.amqp_requests = set()
        try:
            with open(self.fake_amqp) as f:
                for line in f:
                    self.amqp_requests.add(line.strip())
            os.unlink(self.fake_amqp)
        except IOError:
            pass

        try:
            with open(os.path.join(self.data.path, 'data/series-proposed/autopkgtest/pending.json')) as f:
                self.pending_requests = json.load(f)
        except IOError:
                self.pending_requests = None

        self.assertNotIn('FIXME', out)

        return (out, excuses_dict)

    ################################################################
    # Tests for generic packages
    ################################################################

    def test_no_request_for_uninstallable(self):
        '''Does not request a test for an uninstallable package'''

        exc = self.do_test(
            # uninstallable unstable version
            [('lightgreen', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1 (>= 2)'}, 'autopkgtest')],
            {'lightgreen': (False, {})},
            {'lightgreen': [('old-version', '1'), ('new-version', '1.1~beta'),
                            ('reason', 'depends'),
                            ('excuses', 'lightgreen/amd64 unsatisfiable Depends: libgreen1 (>= 2)')
                           ]
            })[1]
        # autopkgtest should not be triggered for uninstallable pkg
        self.assertEqual(exc['lightgreen']['tests'], {})

        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

        with open(os.path.join(self.data.path, 'output', 'series', 'output.txt')) as f:
            upgrade_out = f.read()
        self.assertNotIn('accepted:', upgrade_out)
        self.assertIn('SUCCESS (0/0)', upgrade_out)

    def test_no_wait_for_always_failed_test(self):
        '''We do not need to wait for results for tests which have always failed'''

        # The package has failed before, and with a trigger too on amd64
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (4, 'green 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (4, 'green 1', tr('failedbefore/1')),
        }})

        exc = self.do_test(
            [('darkgreen', {'Version': '2'}, 'autopkgtest')],
            {'darkgreen': (True, {'darkgreen 2': {'i386': 'RUNNING-ALWAYSFAIL',
                                                  'amd64': 'RUNNING-ALWAYSFAIL'}})}
        )[1]

        # the test should still be triggered though
        self.assertEqual(exc['darkgreen']['tests'], {'autopkgtest':
            {'darkgreen 2': {
                'amd64': ['RUNNING-ALWAYSFAIL',
                          'http://autopkgtest.ubuntu.com/running.shtml',
                          'http://autopkgtest.ubuntu.com/packages/d/darkgreen/series/amd64',
                          None,
                          None],
                'i386': ['RUNNING-ALWAYSFAIL',
                         'http://autopkgtest.ubuntu.com/running.shtml',
                         'http://autopkgtest.ubuntu.com/packages/d/darkgreen/series/i386',
                         None,
                         None]}}})

        self.assertEqual(self.pending_requests,
                         {'darkgreen/2': {'darkgreen': ['amd64', 'i386']}})

        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-amd64:darkgreen {"triggers": ["darkgreen/2"]}',
                 'debci-series-i386:darkgreen {"triggers": ["darkgreen/2"]}']))

        with open(os.path.join(self.data.path, 'output', 'series', 'output.txt')) as f:
            upgrade_out = f.read()
        self.assertIn('accepted: darkgreen', upgrade_out)
        self.assertIn('SUCCESS (1/0)', upgrade_out)

    def test_multi_rdepends_with_tests_all_running(self):
        '''Multiple reverse dependencies with tests (all running)'''

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]})

        # we expect the package's and its reverse dependencies' tests to get
        # triggered
        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:green {"triggers": ["green/2"]}',
                 'debci-series-amd64:green {"triggers": ["green/2"]}',
                 'debci-series-i386:lightgreen {"triggers": ["green/2"]}',
                 'debci-series-amd64:lightgreen {"triggers": ["green/2"]}',
                 'debci-series-i386:darkgreen {"triggers": ["green/2"]}',
                 'debci-series-amd64:darkgreen {"triggers": ["green/2"]}']))

        # ... and that they get recorded as pending
        expected_pending = {'green/2': {'darkgreen': ['amd64', 'i386'],
                                        'green': ['amd64', 'i386'],
                                        'lightgreen': ['amd64', 'i386']}}
        self.assertEqual(self.pending_requests, expected_pending)

        # if we run britney again this should *not* trigger any new tests
        self.do_test([], {'green': (False, {})})
        self.assertEqual(self.amqp_requests, set())
        # but the set of pending tests doesn't change
        self.assertEqual(self.pending_requests, expected_pending)

    def test_multi_rdepends_with_tests_all_pass(self):
        '''Multiple reverse dependencies with tests (all pass)'''

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        # first run requests tests and marks them as pending
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]})

        # second run collects the results
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('green/2')),
            # version in testing fails
            'series/i386/g/green/20150101_020000@': (4, 'green 1', tr('green/1')),
            'series/amd64/g/green/20150101_020000@': (4, 'green 1', tr('green/1')),
            # version in unstable succeeds
            'series/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
        }})

        out = self.do_test(
            [],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )[0]

        # all tests ran, there should be no more pending ones
        self.assertEqual(self.pending_requests, {})

        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

        # caches the results and triggers
        with open(os.path.join(self.data.path, 'data/series-proposed/autopkgtest/results.cache')) as f:
            res = json.load(f)
        self.assertEqual(res['green/1']['green']['amd64'],
                         [False, '1', '20150101_020000@'])
        self.assertEqual(set(res['green/2']), {'darkgreen', 'green', 'lightgreen'})
        self.assertEqual(res['green/2']['lightgreen']['i386'],
                         [True, '1', '20150101_100100@'])

        # third run should not trigger any new tests, should all be in the
        # cache
        self.swift.set_results({})
        out = self.do_test(
            [],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             })
            })[0]
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_with_tests_mixed(self):
        '''Multiple reverse dependencies with tests (mixed results)'''

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        # first run requests tests and marks them as pending
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]})

        # second run collects the results
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100201@': (4, 'green 2', tr('green/2')),
            # unrelated results (wrong trigger), ignore this!
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/1')),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('blue/1')),
        }})

        out = self.do_test(
            [],
            {'green': (False, {'green 2': {'amd64': 'ALWAYSFAIL', 'i386': 'PASS'},
                               'lightgreen 1': {'amd64': 'REGRESSION', 'i386': 'RUNNING'},
                               'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'PASS'},
                              })
            })

        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

        # there should be some pending ones
        self.assertEqual(self.pending_requests,
                         {'green/2': {'darkgreen': ['amd64'], 'lightgreen': ['i386']}})

    def test_results_without_triggers(self):
        '''Old results without recorded triggers'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/i386/g/green/20150101_100100@': (0, 'green 1', tr('passedbefore/1')),
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (4, 'green 2'),
        }})

        # none of the above results should be accepted
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              })
            })

        # there should be some pending ones
        self.assertEqual(self.pending_requests,
                         {'green/2': {'lightgreen': ['amd64', 'i386'],
                                      'green': ['amd64', 'i386'],
                                      'darkgreen': ['amd64', 'i386']}})

    def test_multi_rdepends_with_tests_regression(self):
        '''Multiple reverse dependencies with tests (regression)'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/1')),
            'series/amd64/g/green/20150101_100201@': (4, 'green 2', tr('green/2')),
        }})

        out, exc = self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'REGRESSION', 'i386': 'PASS'},
                               'lightgreen 1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )

        # should have links to log and history, but no artifacts (as this is
        # not a PPA)
        self.assertEqual(exc['green']['tests']['autopkgtest']['lightgreen 1']['amd64'][:4],
                ['REGRESSION',
                 'http://localhost:18085/autopkgtest-series/series/amd64/l/lightgreen/20150101_100101@/log.gz',
                 'http://autopkgtest.ubuntu.com/packages/l/lightgreen/series/amd64',
                 None])

        # should have retry link for the regressions (not a stable URL, test
        # seaprately)
        link = urllib.parse.urlparse(exc['green']['tests']['autopkgtest']['lightgreen 1']['amd64'][4])
        self.assertEqual(link.netloc, 'autopkgtest.ubuntu.com')
        self.assertEqual(link.path, '/retry.cgi')
        self.assertEqual(urllib.parse.parse_qs(link.query),
                         {'release': ['series'], 'arch': ['amd64'],
                          'package': ['lightgreen'], 'trigger': ['green/2']})

        # we already had all results before the run, so this should not trigger
        # any new requests
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_with_tests_regression_last_pass(self):
        '''Multiple reverse dependencies with tests (regression), last one passes

        This ensures that we don't just evaluate the test result of the last
        test, but all of them.
        '''
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'series/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/1')),
            'series/amd64/g/green/20150101_100201@': (4, 'green 2', tr('green/2')),
        }})

        out = self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'REGRESSION', 'i386': 'PASS'},
                               'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                               'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )[0]

        self.assertEqual(self.pending_requests, {})
        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_with_tests_always_failed(self):
        '''Multiple reverse dependencies with tests (always failed)'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1')),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1')),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100200@': (4, 'green 2', tr('green/1')),
            'series/amd64/g/green/20150101_100201@': (4, 'green 2', tr('green/2')),
        }})

        out = self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green 2': {'amd64': 'ALWAYSFAIL', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'ALWAYSFAIL', 'i386': 'ALWAYSFAIL'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )[0]

        self.assertEqual(self.pending_requests, {})
        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_arch_specific(self):
        '''Multiple reverse dependencies with arch specific tests'''

        # green has passed before on amd64, doesn't exist on i386
        self.swift.set_results({'autopkgtest-series': {
            'series/amd64/g/green64/20150101_100000@': (0, 'green64 0.1', tr('passedbefore/1')),
        }})

        self.data.add('green64', False, {'Depends': 'libc6 (>= 0.9), libgreen1',
                                         'Architecture': 'amd64'},
                      testsuite='autopkgtest')

        # first run requests tests and marks them as pending
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'lightgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'green64 1': {'amd64': 'RUNNING'},
                              })
            })

        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:green {"triggers": ["green/2"]}',
                 'debci-series-amd64:green {"triggers": ["green/2"]}',
                 'debci-series-i386:lightgreen {"triggers": ["green/2"]}',
                 'debci-series-amd64:lightgreen {"triggers": ["green/2"]}',
                 'debci-series-i386:darkgreen {"triggers": ["green/2"]}',
                 'debci-series-amd64:darkgreen {"triggers": ["green/2"]}',
                 'debci-series-amd64:green64 {"triggers": ["green/2"]}']))

        self.assertEqual(self.pending_requests,
                         {'green/2': {'lightgreen': ['amd64', 'i386'],
                                      'darkgreen': ['amd64', 'i386'],
                                      'green64': ['amd64'],
                                      'green': ['amd64', 'i386']}})

        # second run collects the results
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('green/2')),
            # version in testing fails
            'series/i386/g/green/20150101_020000@': (4, 'green 1', tr('green/1')),
            'series/amd64/g/green/20150101_020000@': (4, 'green 1', tr('green/1')),
            # version in unstable succeeds
            'series/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            # only amd64 result for green64
            'series/amd64/g/green64/20150101_100200@': (0, 'green64 1', tr('green/2')),
        }})

        out = self.do_test(
            [],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'green64 1': {'amd64': 'PASS'},
                             })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )[0]

        # all tests ran, there should be no more pending ones
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_unbuilt(self):
        '''Unbuilt package should not trigger tests or get considered'''

        self.data.add_src('green', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        exc = self.do_test(
            # uninstallable unstable version
            [],
            {'green': (False, {})},
            {'green': [('old-version', '1'), ('new-version', '2'),
                       ('reason', 'no-binaries'),
                       ('excuses', 'green has no up-to-date binaries on any arch')
                      ]
            })[1]
        # autopkgtest should not be triggered for unbuilt pkg
        self.assertEqual(exc['green']['tests'], {})

    def test_rdepends_unbuilt(self):
        '''Unbuilt reverse dependency'''

        # old lightgreen fails, thus new green should be held back
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/1.1')),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/1.1')),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'series/i386/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1.1')),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'series/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1.1')),
            'series/i386/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'series/amd64/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'series/i386/g/green/20150101_100200@': (0, 'green 1.1', tr('green/1.1')),
            'series/amd64/g/green/20150101_100201@': (0, 'green 1.1', tr('green/1.1')),
        }})

        # add unbuilt lightgreen; should run tests against the old version
        self.data.add_src('lightgreen', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        self.do_test(
            [('libgreen1', {'Version': '1.1', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 1.1': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen 1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             'lightgreen': (False, {}),
            },
            {'green': [('old-version', '1'), ('new-version', '1.1')],
             'lightgreen': [('old-version', '1'), ('new-version', '2'),
                            ('excuses', 'lightgreen has no up-to-date binaries on any arch')]
            }
        )

        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # next run should not trigger any new requests
        self.do_test([], {'green': (False, {}), 'lightgreen': (False, {})})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # now lightgreen 2 gets built, should trigger a new test run
        self.data.remove_all(True)
        self.do_test(
            [('libgreen1', {'Version': '1.1', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2'}, 'autopkgtest')],
            {})
        self.assertEqual(self.amqp_requests,
                         set(['debci-series-amd64:lightgreen {"triggers": ["lightgreen/2"]}',
                              'debci-series-i386:lightgreen {"triggers": ["lightgreen/2"]}']))

        # next run collects the results
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100200@': (0, 'lightgreen 2', tr('lightgreen/2')),
            'series/amd64/l/lightgreen/20150101_102000@': (0, 'lightgreen 2', tr('lightgreen/2')),
        }})
        self.do_test(
            [],
            {'green': (True, {'green 1.1': {'amd64': 'PASS', 'i386': 'PASS'},
                              # FIXME: expecting a lightgreen test here
                              # 'lightgreen 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
             'lightgreen': (True, {'lightgreen 2': {'amd64': 'PASS', 'i386': 'PASS'}}),
            },
            {'green': [('old-version', '1'), ('new-version', '1.1')],
             'lightgreen': [('old-version', '1'), ('new-version', '2')],
            }
        )
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_rdepends_unbuilt_unstable_only(self):
        '''Unbuilt reverse dependency which is not in testing'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/2')),
            'series/i386/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'series/amd64/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'series/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
        }})
        # run britney once to pick up previous results
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'}})})

        # add new uninstallable brokengreen; should not run test at all
        exc = self.do_test(
            [('brokengreen', {'Version': '1', 'Depends': 'libgreen1, nonexisting'}, 'autopkgtest')],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'}}),
             'brokengreen': (False, {}),
            },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'brokengreen': [('old-version', '-'), ('new-version', '1'),
                             ('reason', 'depends'),
                             ('excuses', 'brokengreen/amd64 unsatisfiable Depends: nonexisting')],
            })[1]
        # autopkgtest should not be triggered for uninstallable pkg
        self.assertEqual(exc['brokengreen']['tests'], {})

        self.assertEqual(self.amqp_requests, set())

    def test_rdepends_unbuilt_new_version_result(self):
        '''Unbuilt reverse dependency gets test result for newer version

        This might happen if the autopkgtest infrastructure runs the unstable
        source tests against the testing binaries. Even if that gets done
        properly it might still happen that at the time of the britney run the
        package isn't built yet, but it is once the test gets run.
        '''
        # old lightgreen fails, thus new green should be held back
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/1.1')),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/1.1')),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'series/i386/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1.1')),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'series/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1.1')),
            'series/i386/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'series/amd64/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'series/i386/g/green/20150101_100200@': (0, 'green 1.1', tr('green/1.1')),
            'series/amd64/g/green/20150101_100201@': (0, 'green 1.1', tr('green/1.1')),
        }})

        # add unbuilt lightgreen; should run tests against the old version
        self.data.add_src('lightgreen', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        self.do_test(
            [('libgreen1', {'Version': '1.1', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 1.1': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen 1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             'lightgreen': (False, {}),
            },
            {'green': [('old-version', '1'), ('new-version', '1.1')],
             'lightgreen': [('old-version', '1'), ('new-version', '2'),
                            ('excuses', 'lightgreen has no up-to-date binaries on any arch')]
            }
        )
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # lightgreen 2 stays unbuilt in britney, but we get a test result for it
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100200@': (0, 'lightgreen 2', tr('green/1.1')),
            'series/amd64/l/lightgreen/20150101_102000@': (0, 'lightgreen 2', tr('green/1.1')),
        }})
        self.do_test(
            [],
            {'green': (True, {'green 1.1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
             'lightgreen': (False, {}),
            },
            {'green': [('old-version', '1'), ('new-version', '1.1')],
             'lightgreen': [('old-version', '1'), ('new-version', '2'),
                            ('excuses', 'lightgreen has no up-to-date binaries on any arch')]
            }
        )
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # next run should not trigger any new requests
        self.do_test([], {'green': (True, {}), 'lightgreen': (False, {})})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_rdepends_unbuilt_new_version_fail(self):
        '''Unbuilt reverse dependency gets failure for newer version'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('lightgreen/1')),
        }})

        # add unbuilt lightgreen; should request tests against the old version
        self.data.add_src('lightgreen', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'lightgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'darkgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              }),
             'lightgreen': (False, {}),
            },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '2'),
                            ('excuses', 'lightgreen has no up-to-date binaries on any arch')]
            }
        )
        self.assertEqual(len(self.amqp_requests), 6)

        # we only get a result for lightgreen 2, not for the requested 1
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 0.5', tr('green/1')),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 0.5', tr('green/1')),
            'series/i386/l/lightgreen/20150101_100200@': (4, 'lightgreen 2', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100200@': (4, 'lightgreen 2', tr('green/2')),
            'series/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
        }})
        self.do_test(
            [],
            {'green': (False, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen 2': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             'lightgreen': (False, {}),
            },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '2'),
                            ('excuses', 'lightgreen has no up-to-date binaries on any arch')]
            }
        )
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # next run should not trigger any new requests
        self.do_test([], {'green': (False, {}), 'lightgreen': (False, {})})
        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

    def test_package_pair_running(self):
        '''Two packages in unstable that need to go in together (running)'''

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 2)'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              }),
             'lightgreen': (False, {'lightgreen 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'}}),
            },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '2')],
            })

        # we expect the package's and its reverse dependencies' tests to get
        # triggered; lightgreen should be triggered for each trigger
        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:green {"triggers": ["green/2"]}',
                 'debci-series-amd64:green {"triggers": ["green/2"]}',
                 'debci-series-i386:lightgreen {"triggers": ["green/2"]}',
                 'debci-series-amd64:lightgreen {"triggers": ["green/2"]}',
                 'debci-series-i386:lightgreen {"triggers": ["lightgreen/2"]}',
                 'debci-series-amd64:lightgreen {"triggers": ["lightgreen/2"]}',
                 'debci-series-i386:darkgreen {"triggers": ["green/2"]}',
                 'debci-series-amd64:darkgreen {"triggers": ["green/2"]}']))

        # ... and that they get recorded as pending
        self.assertEqual(self.pending_requests,
                         {'lightgreen/2': {'lightgreen': ['amd64', 'i386']},
                          'green/2': {'darkgreen': ['amd64', 'i386'],
                                      'green': ['amd64', 'i386'],
                                      'lightgreen': ['amd64', 'i386']}})

    def test_binary_from_new_source_package_running(self):
        '''building an existing binary for a new source package (running)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'newgreen': (True, {'newgreen 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                 'lightgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                 'darkgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                 }),
            },
            {'newgreen': [('old-version', '-'), ('new-version', '2')]})

        self.assertEqual(len(self.amqp_requests), 8)
        self.assertEqual(self.pending_requests,
                         {'newgreen/2': {'darkgreen': ['amd64', 'i386'],
                                         'green': ['amd64', 'i386'],
                                         'lightgreen': ['amd64', 'i386'],
                                         'newgreen': ['amd64', 'i386']}})

    def test_binary_from_new_source_package_pass(self):
        '''building an existing binary for a new source package (pass)'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('newgreen/2')),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('newgreen/2')),
            'series/i386/g/green/20150101_100000@': (0, 'green 1', tr('newgreen/2')),
            'series/amd64/g/green/20150101_100000@': (0, 'green 1', tr('newgreen/2')),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('newgreen/2')),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('newgreen/2')),
            'series/i386/n/newgreen/20150101_100200@': (0, 'newgreen 2', tr('newgreen/2')),
            'series/amd64/n/newgreen/20150101_100201@': (0, 'newgreen 2', tr('newgreen/2')),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'newgreen': (True, {'newgreen 2': {'amd64': 'PASS', 'i386': 'PASS'},
                                 'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                                 'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                                 'green 1': {'amd64': 'PASS', 'i386': 'PASS'},
                                }),
            },
            {'newgreen': [('old-version', '-'), ('new-version', '2')]})

        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_result_from_older_version(self):
        '''test result from older version than the uploaded one'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('darkgreen/1')),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('darkgreen/1')),
        }})

        self.do_test(
            [('darkgreen', {'Version': '2', 'Depends': 'libc6 (>= 0.9), libgreen1'}, 'autopkgtest')],
            {'darkgreen': (False, {'darkgreen 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'}})})

        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:darkgreen {"triggers": ["darkgreen/2"]}',
                 'debci-series-amd64:darkgreen {"triggers": ["darkgreen/2"]}']))
        self.assertEqual(self.pending_requests,
                         {'darkgreen/2': {'darkgreen': ['amd64', 'i386']}})

        # second run gets the results for darkgreen 2
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100010@': (0, 'darkgreen 2', tr('darkgreen/2')),
            'series/amd64/d/darkgreen/20150101_100010@': (0, 'darkgreen 2', tr('darkgreen/2')),
        }})
        self.do_test(
            [],
            {'darkgreen': (True, {'darkgreen 2': {'amd64': 'PASS', 'i386': 'PASS'}})})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # next run sees a newer darkgreen, should re-run tests
        self.data.remove_all(True)
        self.do_test(
            [('darkgreen', {'Version': '3', 'Depends': 'libc6 (>= 0.9), libgreen1'}, 'autopkgtest')],
            {'darkgreen': (False, {'darkgreen 3': {'amd64': 'RUNNING', 'i386': 'RUNNING'}})})
        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:darkgreen {"triggers": ["darkgreen/3"]}',
                 'debci-series-amd64:darkgreen {"triggers": ["darkgreen/3"]}']))
        self.assertEqual(self.pending_requests,
                         {'darkgreen/3': {'darkgreen': ['amd64', 'i386']}})

    def test_old_result_from_rdep_version(self):
        '''re-runs reverse dependency test on new versions'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 1', tr('green/1')),
            'series/amd64/g/green/20150101_100000@': (0, 'green 1', tr('green/1')),
            'series/i386/g/green/20150101_100010@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100010@': (0, 'green 2', tr('green/2')),
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/2')),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
            })

        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})
        self.data.remove_all(True)

        # second run: new version re-triggers all tests
        self.do_test(
            [('libgreen1', {'Version': '3', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 3': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                              }),
            })

        self.assertEqual(len(self.amqp_requests), 6)
        self.assertEqual(self.pending_requests,
                         {'green/3': {'darkgreen': ['amd64', 'i386'],
                                      'green': ['amd64', 'i386'],
                                      'lightgreen': ['amd64', 'i386']}})

        # third run gets the results for green and lightgreen, darkgreen is
        # still running
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100020@': (0, 'green 3', tr('green/3')),
            'series/amd64/g/green/20150101_100020@': (0, 'green 3', tr('green/3')),
            'series/i386/l/lightgreen/20150101_100010@': (0, 'lightgreen 1', tr('green/3')),
            'series/amd64/l/lightgreen/20150101_100010@': (0, 'lightgreen 1', tr('green/3')),
        }})
        self.do_test(
            [],
            {'green': (False, {'green 3': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                               'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                              }),
            })
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests,
                         {'green/3': {'darkgreen': ['amd64', 'i386']}})

        # fourth run finally gets the new darkgreen result
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100010@': (0, 'darkgreen 1', tr('green/3')),
            'series/amd64/d/darkgreen/20150101_100010@': (0, 'darkgreen 1', tr('green/3')),
        }})
        self.do_test(
            [],
            {'green': (True, {'green 3': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
            })
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_tmpfail(self):
        '''tmpfail results'''

        # one tmpfail result without testpkg-version, should be ignored
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'series/i386/l/lightgreen/20150101_100101@': (16, None, tr('lightgreen/2')),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'series/amd64/l/lightgreen/20150101_100101@': (16, 'lightgreen 2', tr('lightgreen/2')),
        }})

        self.do_test(
            [('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 1)'}, 'autopkgtest')],
            {'lightgreen': (False, {'lightgreen 2': {'amd64': 'REGRESSION', 'i386': 'RUNNING'}})})
        self.assertEqual(self.pending_requests,
                         {'lightgreen/2': {'lightgreen': ['i386']}})

        # one more tmpfail result, should not confuse britney with None version
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100201@': (16, None, tr('lightgreen/2')),
        }})
        self.do_test(
            [],
            {'lightgreen': (False, {'lightgreen 2': {'amd64': 'REGRESSION', 'i386': 'RUNNING'}})})
        with open(os.path.join(self.data.path, 'data/series-proposed/autopkgtest/results.cache')) as f:
            contents = f.read()
        self.assertNotIn('null', contents)
        self.assertNotIn('None', contents)

    def test_rerun_failure(self):
        '''manually re-running failed tests gets picked up'''

        # first run fails
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 2', tr('green/1')),
            'series/i386/g/green/20150101_100101@': (4, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100000@': (0, 'green 2', tr('green/1')),
            'series/amd64/g/green/20150101_100101@': (4, 'green 2', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'lightgreen 1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
            })
        self.assertEqual(self.pending_requests, {})

        # re-running test manually succeeded (note: darkgreen result should be
        # cached already)
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100201@': (0, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100201@': (0, 'lightgreen 1', tr('green/2')),
        }})
        self.do_test(
            [],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
            })
        self.assertEqual(self.pending_requests, {})

    def test_new_runs_dont_clobber_pass(self):
        '''passing once is sufficient

        If a test succeeded once for a particular version and trigger,
        subsequent failures (which might be triggered by other unstable
        uploads) should not invalidate the PASS, as that new failure is the
        fault of the new upload, not the original one.
        '''
        # new libc6 works fine with green
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 1', tr('libc6/2')),
            'series/amd64/g/green/20150101_100000@': (0, 'green 1', tr('libc6/2')),
        }})

        self.do_test(
            [('libc6', {'Version': '2'}, None)],
            {'libc6': (True, {'green 1': {'amd64': 'PASS', 'i386': 'PASS'}})})
        self.assertEqual(self.pending_requests, {})

        # new green fails; that's not libc6's fault though, so it should stay
        # valid
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100100@': (4, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100100@': (4, 'green 2', tr('green/2')),
        }})
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'}}),
             'libc6': (True, {'green 1': {'amd64': 'PASS', 'i386': 'PASS'}}),
            })
        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:darkgreen {"triggers": ["green/2"]}',
                 'debci-series-amd64:darkgreen {"triggers": ["green/2"]}',
                 'debci-series-i386:lightgreen {"triggers": ["green/2"]}',
                 'debci-series-amd64:lightgreen {"triggers": ["green/2"]}',
                ]))

    def test_remove_from_unstable(self):
        '''broken package gets removed from unstable'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100101@': (0, 'green 1', tr('green/1')),
            'series/amd64/g/green/20150101_100101@': (0, 'green 1', tr('green/1')),
            'series/i386/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('green/1')),
            'series/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('green/1')),
            'series/i386/l/lightgreen/20150101_100201@': (4, 'lightgreen 2', tr('green/2 lightgreen/2')),
            'series/amd64/l/lightgreen/20150101_100201@': (4, 'lightgreen 2', tr('green/2 lightgreen/2')),
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 2)'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen 2': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                              }),
            })
        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

        # remove new lightgreen by resetting archive indexes, and re-adding
        # green
        self.data.remove_all(True)

        self.swift.set_results({'autopkgtest-series': {
            # add new result for lightgreen 1
            'series/i386/l/lightgreen/20150101_100301@': (0, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100301@': (0, 'lightgreen 1', tr('green/2')),
        }})

        # next run should re-trigger lightgreen 1 to test against green/2
        exc = self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
            })[1]
        self.assertNotIn('lightgreen 2', exc['green']['tests']['autopkgtest'])

        # should not trigger new requests
        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

        # but the next run should not trigger anything new
        self.do_test(
            [],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
            })
        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

    def test_multiarch_dep(self):
        '''multi-arch dependency'''

        # lightgreen has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('passedbefore/1')),
        }})

        self.data.add('rainbow', False, {'Depends': 'lightgreen:any'},
                      testsuite='autopkgtest')

        self.do_test(
            [('lightgreen', {'Version': '2'}, 'autopkgtest')],
            {'lightgreen': (False, {'lightgreen 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                                    'rainbow 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                   }),
            },
            {'lightgreen': [('old-version', '1'), ('new-version', '2')]}
        )

    def test_nbs(self):
        '''source-less binaries do not cause harm'''

        # NBS in testing
        self.data.add('liboldgreen0', False, add_src=False)
        # NBS in unstable
        self.data.add('liboldgreen1', True, add_src=False)
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green'}, 'autopkgtest')],
            {'green': (True, {'green 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              'lightgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              'darkgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                             }),
            },
            {'green': [('old-version', '1'), ('new-version', '2')]})

    ################################################################
    # Tests for hint processing
    ################################################################

    def test_hint_force_badtest(self):
        '''force-badtest hint'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
        }})

        self.create_hint('pitti', 'force-badtest lightgreen/1')

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
            },
            {'green': [('old-version', '1'), ('new-version', '2'),
                       ('forced-reason', 'badtest lightgreen 1'),
                       ('excuses', 'Should wait for lightgreen 1 test, but forced by pitti')]
            })

    def test_hint_force_badtest_different_version(self):
        '''force-badtest hint with non-matching version'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'series/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'series/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
        }})

        self.create_hint('pitti', 'force-badtest lightgreen/0.1')

        exc = self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen 1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
            },
            {'green': [('reason', 'autopkgtest')]}
        )[1]
        self.assertNotIn('forced-reason', exc['green'])

    def test_hint_force_skiptest(self):
        '''force-skiptest hint'''

        self.create_hint('pitti', 'force-skiptest green/2')

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {}),
            },
            {'green': [('old-version', '1'), ('new-version', '2'),
                       ('forced-reason', 'skiptest'),
                       ('excuses', 'Should wait for tests relating to green 2, but forced by pitti')]
            })

        # should not issue test requests as it's hinted anyway
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_hint_force_skiptest_different_version(self):
        '''force-skiptest hint with non-matching version'''

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        self.create_hint('pitti', 'force-skiptest green/1')
        exc = self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              }),
            },
            {'green': [('reason', 'autopkgtest')]}
        )[1]
        self.assertNotIn('forced-reason', exc['green'])

    ################################################################
    # Kernel related tests
    ################################################################

    def test_detect_dkms_autodep8(self):
        '''DKMS packages are autopkgtested (via autodep8)'''

        self.data.add('dkms', False, {})
        self.data.add('fancy-dkms', False, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'})

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/f/fancy/20150101_100101@': (0, 'fancy 0.1', tr('passedbefore/1'))
        }})

        self.do_test(
            [('dkms', {'Version': '2'}, None)],
            {'dkms': (False, {'fancy 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'}})},
            {'dkms': [('old-version', '1'), ('new-version', '2')]})

    def test_kernel_triggers_dkms(self):
        '''DKMS packages get triggered by kernel uploads'''

        self.data.add('dkms', False, {})
        self.data.add('fancy-dkms', False, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'})

        self.do_test(
            [('linux-image-generic', {'Source': 'linux-meta'}, None),
             ('linux-image-grumpy-generic', {'Source': 'linux-meta-lts-grumpy'}, None),
             ('linux-image-64only', {'Source': 'linux-meta-64only', 'Architecture': 'amd64'}, None),
            ],
            {'linux-meta': (True, {'fancy 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'}}),
             'linux-meta-lts-grumpy': (True, {'fancy 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'}}),
             'linux-meta-64only': (True, {'fancy 1': {'amd64': 'RUNNING-ALWAYSFAIL'}}),
            })

        # one separate test should be triggered for each kernel
        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:fancy {"triggers": ["linux-meta/1"]}',
                 'debci-series-amd64:fancy {"triggers": ["linux-meta/1"]}',
                 'debci-series-i386:fancy {"triggers": ["linux-meta-lts-grumpy/1"]}',
                 'debci-series-amd64:fancy {"triggers": ["linux-meta-lts-grumpy/1"]}',
                 'debci-series-amd64:fancy {"triggers": ["linux-meta-64only/1"]}']))

        # ... and that they get recorded as pending
        self.assertEqual(self.pending_requests,
                         {'linux-meta-lts-grumpy/1': {'fancy': ['amd64', 'i386']},
                          'linux-meta/1': {'fancy': ['amd64', 'i386']},
                          'linux-meta-64only/1': {'fancy': ['amd64']}})

    def test_dkms_results_per_kernel(self):
        '''DKMS results get mapped to the triggering kernel version'''

        self.data.add('dkms', False, {})
        self.data.add('fancy-dkms', False, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'})

        # works against linux-meta and -64only, fails against grumpy i386, no
        # result yet for grumpy amd64
        self.swift.set_results({'autopkgtest-series': {
            'series/amd64/f/fancy/20150101_100301@': (0, 'fancy 0.5', tr('passedbefore/1')),
            'series/i386/f/fancy/20150101_100101@': (0, 'fancy 1', tr('linux-meta/1')),
            'series/amd64/f/fancy/20150101_100101@': (0, 'fancy 1', tr('linux-meta/1')),
            'series/amd64/f/fancy/20150101_100201@': (0, 'fancy 1', tr('linux-meta-64only/1')),
            'series/i386/f/fancy/20150101_100301@': (4, 'fancy 1', tr('linux-meta-lts-grumpy/1')),
        }})

        self.do_test(
            [('linux-image-generic', {'Source': 'linux-meta'}, None),
             ('linux-image-grumpy-generic', {'Source': 'linux-meta-lts-grumpy'}, None),
             ('linux-image-64only', {'Source': 'linux-meta-64only', 'Architecture': 'amd64'}, None),
            ],
            {'linux-meta': (True, {'fancy 1': {'amd64': 'PASS', 'i386': 'PASS'}}),
             'linux-meta-lts-grumpy': (False, {'fancy 1': {'amd64': 'RUNNING', 'i386': 'ALWAYSFAIL'}}),
             'linux-meta-64only': (True, {'fancy 1': {'amd64': 'PASS'}}),
            })

        self.assertEqual(self.pending_requests,
                         {'linux-meta-lts-grumpy/1': {'fancy': ['amd64']}})

    def test_dkms_results_per_kernel_old_results(self):
        '''DKMS results get mapped to the triggering kernel version, old results'''

        self.data.add('dkms', False, {})
        self.data.add('fancy-dkms', False, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'})

        # works against linux-meta and -64only, fails against grumpy i386, no
        # result yet for grumpy amd64
        self.swift.set_results({'autopkgtest-series': {
            # old results without trigger info
            'series/i386/f/fancy/20140101_100101@': (0, 'fancy 1', {}),
            'series/amd64/f/fancy/20140101_100101@': (8, 'fancy 1', {}),
            # current results with triggers
            'series/i386/f/fancy/20150101_100101@': (0, 'fancy 1', tr('linux-meta/1')),
            'series/amd64/f/fancy/20150101_100101@': (0, 'fancy 1', tr('linux-meta/1')),
            'series/amd64/f/fancy/20150101_100201@': (0, 'fancy 1', tr('linux-meta-64only/1')),
            'series/i386/f/fancy/20150101_100301@': (4, 'fancy 1', tr('linux-meta-lts-grumpy/1')),
        }})

        self.do_test(
            [('linux-image-generic', {'Source': 'linux-meta'}, None),
             ('linux-image-grumpy-generic', {'Source': 'linux-meta-lts-grumpy'}, None),
             ('linux-image-64only', {'Source': 'linux-meta-64only', 'Architecture': 'amd64'}, None),
            ],
            {'linux-meta': (True, {'fancy 1': {'amd64': 'PASS', 'i386': 'PASS'}}),
             # we don't have an explicit result for amd64
             'linux-meta-lts-grumpy': (False, {'fancy 1': {'amd64': 'RUNNING', 'i386': 'ALWAYSFAIL'}}),
             'linux-meta-64only': (True, {'fancy 1': {'amd64': 'PASS'}}),
            })

        self.assertEqual(self.pending_requests,
                         {'linux-meta-lts-grumpy/1': {'fancy': ['amd64']}})

    def test_kernel_triggered_tests(self):
        '''linux, lxc, glibc tests get triggered by linux-meta* uploads'''

        self.data.remove_all(False)
        self.data.add('libc6-dev', False, {'Source': 'glibc', 'Depends': 'linux-libc-dev'},
                      testsuite='autopkgtest')
        self.data.add('lxc', False, {'Testsuite-Triggers': 'linux-generic'},
                      testsuite='autopkgtest')
        self.data.add('systemd', False, {'Testsuite-Triggers': 'linux-generic'},
                      testsuite='autopkgtest')
        self.data.add('linux-image-1', False, {'Source': 'linux'}, testsuite='autopkgtest')
        self.data.add('linux-libc-dev', False, {'Source': 'linux'}, testsuite='autopkgtest')
        self.data.add('linux-image', False, {'Source': 'linux-meta', 'Depends': 'linux-image-1'})

        self.swift.set_results({'autopkgtest-series': {
            'series/amd64/l/lxc/20150101_100101@': (0, 'lxc 0.1', tr('passedbefore/1'))
        }})

        exc = self.do_test(
            [('linux-image', {'Version': '2', 'Depends': 'linux-image-2', 'Source': 'linux-meta'}, None),
             ('linux-image-64only', {'Source': 'linux-meta-64only', 'Architecture': 'amd64'}, None),
             ('linux-image-2', {'Version': '2', 'Source': 'linux'}, 'autopkgtest'),
             ('linux-libc-dev', {'Version': '2', 'Source': 'linux'}, 'autopkgtest'),
            ],
            {'linux-meta': (False, {'lxc 1': {'amd64': 'RUNNING', 'i386': 'RUNNING-ALWAYSFAIL'},
                                    'glibc 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                    'linux 2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                    'systemd 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                   }),
             'linux-meta-64only': (False, {'lxc 1': {'amd64': 'RUNNING'}}),
             'linux': (False, {}),
            })[1]
        # the kernel itself should not trigger tests; we want to trigger
        # everything from -meta
        self.assertNotIn('autopkgtest', exc['linux']['tests'])

    def test_kernel_waits_on_meta(self):
        '''linux waits on linux-meta'''

        self.data.add('dkms', False, {})
        self.data.add('fancy-dkms', False, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'})
        self.data.add('linux-image-generic', False, {'Version': '0.1', 'Source': 'linux-meta', 'Depends': 'linux-image-1'})
        self.data.add('linux-image-1', False, {'Source': 'linux'}, testsuite='autopkgtest')
        self.data.add('linux-firmware', False, {'Source': 'linux-firmware'}, testsuite='autopkgtest')

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/f/fancy/20150101_090000@': (0, 'fancy 0.5', tr('passedbefore/1')),
            'series/i386/l/linux/20150101_100000@': (0, 'linux 2', tr('linux-meta/0.2')),
            'series/amd64/l/linux/20150101_100000@': (0, 'linux 2', tr('linux-meta/0.2')),
            'series/i386/l/linux-firmware/20150101_100000@': (0, 'linux-firmware 2', tr('linux-firmware/2')),
            'series/amd64/l/linux-firmware/20150101_100000@': (0, 'linux-firmware 2', tr('linux-firmware/2')),
        }})

        self.do_test(
            [('linux-image-generic', {'Version': '0.2', 'Source': 'linux-meta', 'Depends': 'linux-image-2'}, None),
             ('linux-image-2', {'Version': '2', 'Source': 'linux'}, 'autopkgtest'),
             ('linux-firmware', {'Version': '2', 'Source': 'linux-firmware'}, 'autopkgtest'),
            ],
            {'linux-meta': (False, {'fancy 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                                    'linux 2': {'amd64': 'PASS', 'i386': 'PASS'}
                                   }),
             # no tests, but should wait on linux-meta
             'linux': (False, {}),
             # this one does not have a -meta, so don't wait
             'linux-firmware': (True, {'linux-firmware 2': {'amd64': 'PASS', 'i386': 'PASS'}}),
            },
            {'linux': [('reason', 'depends'),
                       ('excuses', 'Depends: linux linux-meta (not considered)')]
            }
        )

        # now linux-meta is ready to go
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/f/fancy/20150101_100000@': (0, 'fancy 1', tr('linux-meta/0.2')),
            'series/amd64/f/fancy/20150101_100000@': (0, 'fancy 1', tr('linux-meta/0.2')),
        }})
        self.do_test(
            [],
            {'linux-meta': (True, {'fancy 1': {'amd64': 'PASS', 'i386': 'PASS'},
                                   'linux 2': {'amd64': 'PASS', 'i386': 'PASS'}}),
             'linux': (True, {}),
             'linux-firmware': (True, {'linux-firmware 2': {'amd64': 'PASS', 'i386': 'PASS'}}),
            },
            {'linux': [('excuses', 'Depends: linux linux-meta')]
            }
        )

    ################################################################
    # Tests for special-cased packages
    ################################################################

    def test_gcc(self):
        '''gcc only triggers some key packages'''

        self.data.add('binutils', False, {}, testsuite='autopkgtest')
        self.data.add('linux', False, {}, testsuite='autopkgtest')
        self.data.add('notme', False, {'Depends': 'libgcc1'}, testsuite='autopkgtest')

        # binutils has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/b/binutils/20150101_100000@': (0, 'binutils 1', tr('passedbefore/1')),
        }})

        exc = self.do_test(
            [('libgcc1', {'Source': 'gcc-5', 'Version': '2'}, None)],
            {'gcc-5': (False, {'binutils 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'linux 1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'}})})[1]
        self.assertNotIn('notme 1', exc['gcc-5']['tests']['autopkgtest'])

    def test_alternative_gcc(self):
        '''alternative gcc does not trigger anything'''

        self.data.add('binutils', False, {}, testsuite='autopkgtest')
        self.data.add('notme', False, {'Depends': 'libgcc1'}, testsuite='autopkgtest')

        exc = self.do_test(
            [('libgcc1', {'Source': 'gcc-snapshot', 'Version': '2'}, None)],
            {'gcc-snapshot': (True, {})})[1]
        self.assertNotIn('autopkgtest', exc['gcc-snapshot']['tests'])

    ################################################################
    # Tests for non-default ADT_* configuration modes
    ################################################################

    def test_disable_adt(self):
        '''Run without autopkgtest requests'''

        # Disable AMQP server config, to ensure we don't touch them with ADT
        # disabled
        for line in fileinput.input(self.britney_conf, inplace=True):
            if line.startswith('ADT_ENABLE'):
                print('ADT_ENABLE = no')
            elif not line.startswith('ADT_AMQP') and not line.startswith('ADT_SWIFT_URL'):
                sys.stdout.write(line)

        exc = self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {})},
            {'green': [('old-version', '1'), ('new-version', '2')]})[1]
        self.assertNotIn('autopkgtest', exc['green']['tests'])

        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, None)

    def test_ppas(self):
        '''Run test requests with additional PPAs'''

        for line in fileinput.input(self.britney_conf, inplace=True):
            if line.startswith('ADT_PPAS'):
                print('ADT_PPAS = joe/foo awesome-developers/staging')
            else:
                sys.stdout.write(line)

        exc = self.do_test(
            [('lightgreen', {'Version': '2'}, 'autopkgtest')],
            {'lightgreen': (True, {'lightgreen 2': {'amd64': 'RUNNING-ALWAYSFAIL'}})},
            {'lightgreen': [('old-version', '1'), ('new-version', '2')]}
        )[1]
        self.assertEqual(exc['lightgreen']['tests'], {'autopkgtest':
            {'lightgreen 2': {
                'amd64': ['RUNNING-ALWAYSFAIL',
                          'http://autopkgtest.ubuntu.com/running.shtml',
                          None,
                          None,
                          None],
                'i386': ['RUNNING-ALWAYSFAIL',
                         'http://autopkgtest.ubuntu.com/running.shtml',
                         None,
                         None,
                         None]}
            }})

        for arch in ['i386', 'amd64']:
            self.assertTrue('debci-series-%s:lightgreen {"triggers": ["lightgreen/2"], "ppas": ["joe/foo", "awesome-developers/staging"]}' % arch in self.amqp_requests or
                            'debci-series-%s:lightgreen {"ppas": ["joe/foo", "awesome-developers/staging"], "triggers": ["lightgreen/2"]}' % arch in self.amqp_requests)
        self.assertEqual(len(self.amqp_requests), 2)

        # add results to PPA specific swift container
        self.swift.set_results({'autopkgtest-series-awesome-developers-staging': {
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 2', tr('lightgreen/2')),
            'series/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 2', tr('lightgreen/2')),
        }})

        exc = self.do_test(
            [],
            {'lightgreen': (True, {'lightgreen 2': {'i386': 'PASS', 'amd64': 'PASS'}})},
            {'lightgreen': [('old-version', '1'), ('new-version', '2')]}
        )[1]
        self.assertEqual(exc['lightgreen']['tests'], {'autopkgtest':
            {'lightgreen 2': {
                'amd64': ['PASS',
                          'http://localhost:18085/autopkgtest-series-awesome-developers-staging/series/amd64/l/lightgreen/20150101_100101@/log.gz',
                          None,
                          'http://localhost:18085/autopkgtest-series-awesome-developers-staging/series/amd64/l/lightgreen/20150101_100101@/artifacts.tar.gz',
                          None],
                'i386': ['PASS',
                         'http://localhost:18085/autopkgtest-series-awesome-developers-staging/series/i386/l/lightgreen/20150101_100100@/log.gz',
                         None,
                         'http://localhost:18085/autopkgtest-series-awesome-developers-staging/series/i386/l/lightgreen/20150101_100100@/artifacts.tar.gz',
                         None]}
            }})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_disable_upgrade_tester(self):
        '''Run without second stage upgrade tester'''

        for line in fileinput.input(self.britney_conf, inplace=True):
            if not line.startswith('UPGRADE_OUTPUT'):
                sys.stdout.write(line)

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {})[1]

        self.assertFalse(os.path.exists(os.path.join(self.data.path, 'output', 'series', 'output.txt')))

    def test_shared_results_cache(self):
        '''Run with shared r/o results.cache'''

        # first run to create results.cache
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 2', tr('lightgreen/2')),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 2', tr('lightgreen/2')),
        }})

        self.do_test(
            [('lightgreen', {'Version': '2', 'Depends': 'libc6'}, 'autopkgtest')],
            {'lightgreen': (True, {'lightgreen 2': {'i386': 'PASS', 'amd64': 'PASS'}})},
            )

        # move and remember original contents
        local_path = os.path.join(self.data.path, 'data/series-proposed/autopkgtest/results.cache')
        shared_path = os.path.join(self.data.path, 'shared_results.cache')
        os.rename(local_path, shared_path)
        with open(shared_path) as f:
            orig_contents = f.read()

        # enable shared cache
        for line in fileinput.input(self.britney_conf, inplace=True):
            if 'ADT_SHARED_RESULTS_CACHE' in line:
                print('ADT_SHARED_RESULTS_CACHE = %s' % shared_path)
            else:
                sys.stdout.write(line)

        # second run, should now not update cache
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 3', tr('lightgreen/3')),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 3', tr('lightgreen/3')),
        }})

        self.data.remove_all(True)
        self.do_test(
            [('lightgreen', {'Version': '3', 'Depends': 'libc6'}, 'autopkgtest')],
            {'lightgreen': (True, {'lightgreen 3': {'i386': 'PASS', 'amd64': 'PASS'}})},
            )

        # leaves results.cache untouched
        self.assertFalse(os.path.exists(local_path))
        with open(shared_path) as f:
            self.assertEqual(orig_contents, f.read())


if __name__ == '__main__':
    unittest.main()
