#!/usr/bin/python3
# (C) 2014 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import apt_pkg
import os
import sys
import fileinput
import unittest
import json
import pprint

import yaml

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from tests import TestBase, mock_swift

apt_pkg.init()


class TestAutoPkgTest(TestBase):
    '''AMQP/cloud interface'''

    def setUp(self):
        super(TestAutoPkgTest, self).setUp()
        self.fake_amqp = os.path.join(self.data.path, 'amqp')

        # Disable boottests and set fake AMQP and Swift server
        for line in fileinput.input(self.britney_conf, inplace=True):
            if 'BOOTTEST_ENABLE' in line:
                print('BOOTTEST_ENABLE   = no')
            elif 'ADT_AMQP' in line:
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
                                           'Depends': 'libc6 (>= 0.9)'})
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
            self.assertEqual(excuses_dict[src]['is-candidate'], is_candidate)
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
            with open(os.path.join(self.data.path, 'data/series-proposed/autopkgtest/pending.txt')) as f:
                self.pending_requests = f.read()
        except IOError:
                self.pending_requests = None

        self.assertNotIn('FIXME', out)

        return (out, excuses_dict)

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

        self.assertEqual(self.pending_requests, '')
        self.assertEqual(self.amqp_requests, set())

    def test_multi_rdepends_with_tests_all_running(self):
        '''Multiple reverse dependencies with tests (all running)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
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
        expected_pending = '''darkgreen 1 amd64 green 2
darkgreen 1 i386 green 2
green 2 amd64 green 2
green 2 i386 green 2
lightgreen 1 amd64 green 2
lightgreen 1 i386 green 2
'''
        self.assertEqual(self.pending_requests, expected_pending)

        # if we run britney again this should *not* trigger any new tests
        self.do_test([], {'green': (False, {})})
        self.assertEqual(self.amqp_requests, set())
        # but the set of pending tests doesn't change
        self.assertEqual(self.pending_requests, expected_pending)

    def test_multi_rdepends_with_tests_all_pass(self):
        '''Multiple reverse dependencies with tests (all pass)'''

        # first run requests tests and marks them as pending
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                              })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]})

        # second run collects the results
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1'),
            # version in testing fails
            'series/i386/g/green/20150101_020000@': (4, 'green 1'),
            'series/amd64/g/green/20150101_020000@': (4, 'green 1'),
            # version in unstable succeeds
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2'),
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
        self.assertEqual(self.pending_requests, '')

        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

        # caches the results and triggers
        with open(os.path.join(self.data.path, 'data/series-proposed/autopkgtest/results.cache')) as f:
            res = json.load(f)
        self.assertEqual(res['green']['i386'],
                         ['20150101_100200@',
                          {'1': [False, []], '2': [True, [['green', '2']]]},
                          True])
        self.assertEqual(res['lightgreen']['amd64'],
                         ['20150101_100101@',
                          {'1': [True, [['green', '2']]]},
                          True])

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
        self.assertEqual(self.pending_requests, '')
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_with_tests_mixed(self):
        '''Multiple reverse dependencies with tests (mixed results)'''

        # first run requests tests and marks them as pending
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                              })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]})

        # second run collects the results
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (4, 'green 2'),
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
        self.assertIn('darkgreen 1 amd64 green 2', self.pending_requests)
        self.assertIn('lightgreen 1 i386 green 2', self.pending_requests)

    def test_multi_rdepends_with_tests_regression(self):
        '''Multiple reverse dependencies with tests (regression)'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (4, 'green 2'),
        }})

        out = self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'REGRESSION', 'i386': 'PASS'},
                               'lightgreen 1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              })
            },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )[0]

        self.assertEqual(self.pending_requests, '')
        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_with_tests_regression_last_pass(self):
        '''Multiple reverse dependencies with tests (regression), last one passes

        This ensures that we don't just evaluate the test result of the last
        test, but all of them.
        '''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (4, 'green 2'),
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

        self.assertEqual(self.pending_requests, '')
        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_with_tests_always_failed(self):
        '''Multiple reverse dependencies with tests (always failed)'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (4, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100200@': (4, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (4, 'green 2'),
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

        self.assertEqual(self.pending_requests, '')
        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_arch_specific(self):
        '''Multiple reverse dependencies with arch specific tests'''

        self.data.add('green64', False, {'Depends': 'libc6 (>= 0.9), libgreen1',
                                         'Architecture': 'amd64'},
                      testsuite='autopkgtest')

        # first run requests tests and marks them as pending
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
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

        self.assertIn('green64 1 amd64', self.pending_requests)
        self.assertNotIn('green64 1 i386', self.pending_requests)

        # second run collects the results
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1'),
            # version in testing fails
            'series/i386/g/green/20150101_020000@': (4, 'green 1'),
            'series/amd64/g/green/20150101_020000@': (4, 'green 1'),
            # version in unstable succeeds
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2'),
            # only amd64 result for green64
            'series/amd64/g/green64/20150101_100200@': (0, 'green64 1'),
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
        self.assertEqual(self.pending_requests, '')

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
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (4, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1'),
            'series/i386/g/green/20150101_020000@': (0, 'green 1'),
            'series/amd64/g/green/20150101_020000@': (0, 'green 1'),
            'series/i386/g/green/20150101_100200@': (0, 'green 1.1'),
            'series/amd64/g/green/20150101_100201@': (0, 'green 1.1'),
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

        self.assertEqual(len(self.amqp_requests), 6)
        self.assertEqual(self.pending_requests, '')

        # next run should not trigger any new requests
        self.do_test([], {'green': (False, {}), 'lightgreen': (False, {})})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, '')

        # now lightgreen 2 gets built, should trigger a new test run
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100200@': (0, 'lightgreen 2'),
            'series/amd64/l/lightgreen/20150101_102000@': (0, 'lightgreen 2'),
        }})
        self.data.remove_all(True)
        self.do_test(
            [('libgreen1', {'Version': '1.1', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2'}, 'autopkgtest')],
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
        self.assertEqual(self.amqp_requests,
                         set(['debci-series-amd64:lightgreen {"triggers": ["lightgreen/2"]}',
                              'debci-series-i386:lightgreen {"triggers": ["lightgreen/2"]}']))
        self.assertEqual(self.pending_requests, '')

    def test_rdepends_unbuilt_unstable_only(self):
        '''Unbuilt reverse dependency which is not in testing'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/i386/g/green/20150101_020000@': (0, 'green 1'),
            'series/amd64/g/green/20150101_020000@': (0, 'green 1'),
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2'),
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
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (4, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1'),
            'series/i386/g/green/20150101_020000@': (0, 'green 1'),
            'series/amd64/g/green/20150101_020000@': (0, 'green 1'),
            'series/i386/g/green/20150101_100200@': (0, 'green 1.1'),
            'series/amd64/g/green/20150101_100201@': (0, 'green 1.1'),
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
        self.assertEqual(len(self.amqp_requests), 6)
        self.assertEqual(self.pending_requests, '')

        # lightgreen 2 stays unbuilt in britney, but we get a test result for it
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100200@': (0, 'lightgreen 2'),
            'series/amd64/l/lightgreen/20150101_102000@': (0, 'lightgreen 2'),
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
        self.assertEqual(self.pending_requests, '')

        # next run should not trigger any new requests
        self.do_test([], {'green': (True, {}), 'lightgreen': (False, {})})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, '')

    def test_rdepends_unbuilt_new_version_fail(self):
        '''Unbuilt reverse dependency gets failure for newer version'''

        # add unbuilt lightgreen; should request tests against the old version
        self.data.add_src('lightgreen', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
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
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 0.5'),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 0.5'),
            'series/i386/l/lightgreen/20150101_100200@': (4, 'lightgreen 2'),
            'series/amd64/l/lightgreen/20150101_100200@': (4, 'lightgreen 2'),
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2'),
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
        self.assertEqual(self.pending_requests, '')

        # next run should not trigger any new requests
        self.do_test([], {'green': (False, {}), 'lightgreen': (False, {})})
        self.assertEqual(self.pending_requests, '')
        self.assertEqual(self.amqp_requests, set())

    def test_hint_force_badtest(self):
        '''force-badtest hint'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100200@': (0, 'green 2'),
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
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100200@': (0, 'green 2'),
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

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                              'lightgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                              'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                             }),
            },
            {'green': [('old-version', '1'), ('new-version', '2'),
                       ('forced-reason', 'skiptest'),
                       ('excuses', 'Should wait for tests relating to green 2, but forced by pitti')]
            })

    def test_hint_force_skiptest_different_version(self):
        '''force-skiptest hint with non-matching version'''

        self.create_hint('pitti', 'force-skiptest green/1')
        exc = self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'lightgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                              }),
            },
            {'green': [('reason', 'autopkgtest')]}
        )[1]
        self.assertNotIn('forced-reason', exc['green'])

    def test_package_pair_running(self):
        '''Two packages in unstable that need to go in together (running)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 2)'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'lightgreen 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                              }),
             'lightgreen': (False, {'lightgreen 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'}}),
            },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '2')],
            })

        # we expect the package's and its reverse dependencies' tests to get
        # triggered; lightgreen should be triggered only once
        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:green {"triggers": ["green/2"]}',
                 'debci-series-amd64:green {"triggers": ["green/2"]}',
                 'debci-series-i386:lightgreen {"triggers": ["green/2", "lightgreen/2"]}',
                 'debci-series-amd64:lightgreen {"triggers": ["green/2", "lightgreen/2"]}',
                 'debci-series-i386:darkgreen {"triggers": ["green/2"]}',
                 'debci-series-amd64:darkgreen {"triggers": ["green/2"]}']))

        # ... and that they get recorded as pending
        expected_pending = '''darkgreen 1 amd64 green 2
darkgreen 1 i386 green 2
green 2 amd64 green 2
green 2 i386 green 2
lightgreen 2 amd64 green 2
lightgreen 2 amd64 lightgreen 2
lightgreen 2 i386 green 2
lightgreen 2 i386 lightgreen 2
'''
        self.assertEqual(self.pending_requests, expected_pending)

    def test_binary_from_new_source_package_running(self):
        '''building an existing binary for a new source package (running)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'newgreen': (False, {'newgreen 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                                  'lightgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                                  'darkgreen 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                                 }),
            },
            {'newgreen': [('old-version', '-'), ('new-version', '2')]})

        self.assertEqual(len(self.amqp_requests), 6)
        expected_pending = '''darkgreen 1 amd64 newgreen 2
darkgreen 1 i386 newgreen 2
lightgreen 1 amd64 newgreen 2
lightgreen 1 i386 newgreen 2
newgreen 2 amd64 newgreen 2
newgreen 2 i386 newgreen 2
'''
        self.assertEqual(self.pending_requests, expected_pending)

    def test_binary_from_new_source_package_pass(self):
        '''building an existing binary for a new source package (pass)'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'series/i386/n/newgreen/20150101_100200@': (0, 'newgreen 2'),
            'series/amd64/n/newgreen/20150101_100201@': (0, 'newgreen 2'),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'newgreen': (True, {'newgreen 2': {'amd64': 'PASS', 'i386': 'PASS'},
                                 'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                                 'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                                }),
            },
            {'newgreen': [('old-version', '-'), ('new-version', '2')]})

        self.assertEqual(len(self.amqp_requests), 6)
        self.assertEqual(self.pending_requests, '')

    def test_result_from_older_version(self):
        '''test result from older version than the uploaded one'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
        }})

        self.do_test(
            [('darkgreen', {'Version': '2', 'Depends': 'libc6 (>= 0.9), libgreen1'}, 'autopkgtest')],
            {'darkgreen': (False, {'darkgreen 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'}})})

        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:darkgreen {"triggers": ["darkgreen/2"]}',
                 'debci-series-amd64:darkgreen {"triggers": ["darkgreen/2"]}']))
        self.assertEqual(self.pending_requests,
                         'darkgreen 2 amd64 darkgreen 2\ndarkgreen 2 i386 darkgreen 2\n')

        # second run gets the results for darkgreen 2
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/i386/d/darkgreen/20150101_100010@': (0, 'darkgreen 2'),
            'series/amd64/d/darkgreen/20150101_100010@': (0, 'darkgreen 2'),
        }})
        self.do_test(
            [],
            {'darkgreen': (True, {'darkgreen 2': {'amd64': 'PASS', 'i386': 'PASS'}})})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, '')

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
                         'darkgreen 3 amd64 darkgreen 3\ndarkgreen 3 i386 darkgreen 3\n')

    def test_old_result_from_rdep_version(self):
        '''re-runs reverse dependency test on new versions'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 1'),
            'series/amd64/g/green/20150101_100000@': (0, 'green 1'),
            'series/i386/g/green/20150101_100010@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100010@': (0, 'green 2'),
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
            })

        self.assertEqual(len(self.amqp_requests), 6)
        self.assertEqual(self.pending_requests, '')
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

        expected_pending = '''darkgreen 1 amd64 green 3
darkgreen 1 i386 green 3
green 3 amd64 green 3
green 3 i386 green 3
lightgreen 1 amd64 green 3
lightgreen 1 i386 green 3
'''
        self.assertEqual(self.pending_requests, expected_pending)

        # third run gets the results for green and lightgreen, darkgreen is
        # still running
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100020@': (0, 'green 3'),
            'series/amd64/g/green/20150101_100020@': (0, 'green 3'),
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/i386/l/lightgreen/20150101_100010@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100010@': (0, 'lightgreen 1'),
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
                         'darkgreen 1 amd64 green 3\ndarkgreen 1 i386 green 3\n')

        # fourth run finally gets the new darkgreen result
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100010@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100010@': (0, 'darkgreen 1'),
        }})
        self.do_test(
            [],
            {'green': (True, {'green 3': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
            })
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, '')

    def test_tmpfail(self):
        '''tmpfail results'''

        # one tmpfail result without testpkg-version, should be ignored
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100101@': (16, None),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (16, 'lightgreen 2'),
        }})

        self.do_test(
            [('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 1)'}, 'autopkgtest')],
            {'lightgreen': (False, {'lightgreen 2': {'amd64': 'REGRESSION', 'i386': 'RUNNING'}})})
        self.assertEqual(self.pending_requests, 'lightgreen 2 i386 lightgreen 2\n')

        # one more tmpfail result, should not confuse britney with None version
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100201@': (16, None),
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
            'series/i386/g/green/20150101_100000@': (0, 'green 2'),
            'series/i386/g/green/20150101_100101@': (4, 'green 2'),
            'series/amd64/g/green/20150101_100000@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100101@': (4, 'green 2'),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1'),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'lightgreen 1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
            })
        self.assertEqual(self.pending_requests, '')

        # re-running test manually succeeded (note: darkgreen result should be
        # cached already)
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100000@': (0, 'green 2'),
            'series/i386/g/green/20150101_100101@': (4, 'green 2'),
            'series/amd64/g/green/20150101_100000@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100101@': (4, 'green 2'),
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),

            'series/i386/g/green/20150101_100201@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2'),
            'series/i386/l/lightgreen/20150101_100201@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100201@': (0, 'lightgreen 1'),
        }})
        self.do_test(
            [],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
            })
        self.assertEqual(self.pending_requests, '')

    def test_remove_from_unstable(self):
        '''broken package gets removed from unstable'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100101@': (0, 'green 1'),
            'series/amd64/g/green/20150101_100101@': (0, 'green 1'),
            'series/i386/g/green/20150101_100201@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2'),
            'series/i386/l/lightgreen/20150101_100101@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100201@': (4, 'lightgreen 2'),
            'series/amd64/l/lightgreen/20150101_100201@': (4, 'lightgreen 2'),
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1'),
        }})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 2)'}, 'autopkgtest')],
            {'green': (False, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen 2': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                              }),
            })
        self.assertEqual(self.pending_requests, '')

        # remove new lightgreen by resetting archive indexes, and re-adding
        # green
        self.data.remove_all(True)

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/g/green/20150101_100101@': (0, 'green 1'),
            'series/amd64/g/green/20150101_100101@': (0, 'green 1'),
            'series/i386/g/green/20150101_100201@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (0, 'green 2'),
            'series/i386/l/lightgreen/20150101_100101@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100201@': (4, 'lightgreen 2'),
            'series/amd64/l/lightgreen/20150101_100201@': (4, 'lightgreen 2'),
            # add new result for lightgreen 1
            'series/i386/l/lightgreen/20150101_100301@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100301@': (0, 'lightgreen 1'),
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1'),
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
        self.assertEqual(self.pending_requests, '')
        self.assertEqual(self.amqp_requests,
                         set(['debci-series-amd64:lightgreen {"triggers": ["green/2"]}',
                              'debci-series-i386:lightgreen {"triggers": ["green/2"]}']))

        # but the next run should not trigger anything new
        self.do_test(
            [],
            {'green': (True, {'green 2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen 1': {'amd64': 'PASS', 'i386': 'PASS'},
                             }),
            })
        self.assertEqual(self.pending_requests, '')
        self.assertEqual(self.amqp_requests, set())

    def test_multiarch_dep(self):
        '''multi-arch dependency'''

        self.data.add('rainbow', False, {'Depends': 'lightgreen:any'},
                      testsuite='autopkgtest')

        self.do_test(
            [('lightgreen', {'Version': '2'}, 'autopkgtest')],
            {'lightgreen': (False, {'lightgreen 2': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                                    'rainbow 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                                   }),
            },
            {'lightgreen': [('old-version', '1'), ('new-version', '2')]}
        )

    def test_detect_dkms_autodep8(self):
        '''DKMS packages are autopkgtested (via autodep8)'''

        self.data.add('dkms', False, {})
        self.data.add('fancy-dkms', False, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'})

        self.do_test(
            [('dkms', {'Version': '2'}, None)],
            {'dkms': (False, {'fancy 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'}})},
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
            {'linux-meta': (False, {'fancy 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'}}),
             'linux-meta-lts-grumpy': (False, {'fancy 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'}}),
             'linux-meta-64only': (False, {'fancy 1': {'amd64': 'RUNNING'}}),
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
        expected_pending = '''fancy 1 amd64 linux-meta 1
fancy 1 amd64 linux-meta-64only 1
fancy 1 amd64 linux-meta-lts-grumpy 1
fancy 1 i386 linux-meta 1
fancy 1 i386 linux-meta-lts-grumpy 1
'''
        self.assertEqual(self.pending_requests, expected_pending)

    def test_kernel_triggers_lxc(self):
        '''LXC test gets triggered by kernel uploads'''

        self.data.add('lxc', False, {'Testsuite-Triggers': 'linux-generic'},
                      testsuite='autopkgtest')

        self.do_test(
            [('linux-generic', {'Source': 'linux-meta'}, None)],
            {'linux-meta': (False, {'lxc 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'}})})

    def test_gcc(self):
        '''gcc only triggers some key packages'''

        self.data.add('binutils', False, {}, testsuite='autopkgtest')
        self.data.add('linux', False, {}, testsuite='autopkgtest')
        self.data.add('notme', False, {'Depends': 'libgcc1'}, testsuite='autopkgtest')

        exc = self.do_test(
            [('libgcc1', {'Source': 'gcc-5', 'Version': '2'}, None)],
            {'gcc-5': (False, {'binutils 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'linux 1': {'amd64': 'RUNNING', 'i386': 'RUNNING'}})})[1]
        self.assertNotIn('notme 1', exc['gcc-5']['tests']['autopkgtest'])

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


if __name__ == '__main__':
    unittest.main()
