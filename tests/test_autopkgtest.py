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
import sqlite3
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


ON_ALL_ARCHES = {'on-architectures': ['amd64', 'arm64', 'armhf', 'i386', 'powerpc', 'ppc64el'],
                 'on-unimportant-architectures': []}


class TestAutopkgtestBase(TestBase):
    '''AMQP/cloud interface'''

    ################################################################
    # Common test code
    ################################################################

    def setUp(self):
        super().setUp()
        self.fake_amqp = os.path.join(self.data.path, 'amqp')
        self.db_path = os.path.join(self.data.path, 'autopkgtest.db')

        # Set fake AMQP and Swift server and autopkgtest.db
        for line in fileinput.input(self.britney_conf, inplace=True):
            if 'ADT_AMQP' in line:
                print('ADT_AMQP = file://%s' % self.fake_amqp)
            elif 'ADT_DB_URL' in line:
                print('ADT_DB_URL = file://%s' % self.db_path)
            else:
                sys.stdout.write(line)

        # Set up sourceppa cache for testing
        self.sourceppa_cache = {
            'gcc-5': {'2': ''},
            'gcc-snapshot': {'2': ''},
            'green': {'2': '', '1.1': '', '3': ''},
            'lightgreen': {'2': '', '1.1~beta': '', '3': ''},
            'linux-meta-64only': {'1': ''},
            'linux-meta-lts-grumpy': {'1': ''},
            'linux-meta': {'0.2': '', '1': '', '2': ''},
            'linux': {'2': ''},
            'newgreen': {'2': ''},
        }

        self.email_cache = {}
        for pkg, vals in self.sourceppa_cache.items():
            for version, empty in vals.items():
                self.email_cache.setdefault(pkg, {})
                self.email_cache[pkg][version] = True

        self.email_cache = {}
        for pkg, vals in self.sourceppa_cache.items():
            for version, empty in vals.items():
                self.email_cache.setdefault(pkg, {})
                self.email_cache[pkg][version] = True

        # create mock Swift server (but don't start it yet, as tests first need
        # to poke in results)
        self.swift = mock_swift.AutoPkgTestSwiftServer(port=18085)
        self.swift.set_results({})

        self.db = self.init_sqlite_db(self.db_path)

    def tearDown(self):
        del self.swift
        self.db.close()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError: pass

    # https://git.launchpad.net/autopkgtest-cloud/tree/charms/focal/autopkgtest-web/webcontrol/publish-db,
    # https://git.launchpad.net/autopkgtest-cloud/tree/charms/focal/autopkgtest-web/webcontrol/helpers/utils.py
    def init_sqlite_db(self, path):
        """Create DB if it does not exist, and connect to it"""

        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode = MEMORY")
        db.execute(
            "CREATE TABLE current_version("
            "  release CHAR[20], "
            "  pocket CHAR[40], "
            "  component CHAR[10],"
            "  package CHAR[50], "
            "  version CHAR[120], "
            "  PRIMARY KEY(release, package))"
        )
        db.execute("CREATE INDEX IF NOT EXISTS current_version_pocket_ix "
                   "ON current_version(pocket, component)")

        db.execute(
            "CREATE TABLE url_last_checked("
            "  url CHAR[100], "
            "  timestamp CHAR[50], "
            "  PRIMARY KEY(url))"
        )

        db.execute('CREATE TABLE IF NOT EXISTS test ('
                   '  id INTEGER PRIMARY KEY, '
                   '  release CHAR[20], '
                   '  arch CHAR[20], '
                   '  package char[120])')
        db.execute('CREATE TABLE IF NOT EXISTS result ('
                   '  test_id INTEGER, '
                   '  run_id CHAR[30], '
                   '  version VARCHAR[200], '
                   '  triggers TEXT, '
                   '  duration INTEGER, '
                   '  exitcode INTEGER, '
                   '  requester TEXT, '
                   '  PRIMARY KEY(test_id, run_id), '
                   '  FOREIGN KEY(test_id) REFERENCES test(id))')
        # /packages/<name> mostly benefits from the index on package (0.8s -> 0.01s),
        # but adding the other fields improves it a further 50% to 0.005s.
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS test_package_uix ON test('
                   '  package, release, arch)')
        db.execute('CREATE INDEX IF NOT EXISTS result_run_ix ON result('
                   '  run_id desc)')

        db.commit()
        return db

    def set_results(self, results):
        '''Wrapper to set autopkgtest results in both swift and sqlite3'''
        self.swift.set_results(results)

        # swift bucket name is irrelevant for sqlite
        for i in results.values():
            for k,v in i.items():
                (series, arch, discard, source, latest) = k.split('/')
                retcode = v[0]
                if not v[1]:
                    source_ver = None
                else:
                    source_ver = v[1].split(' ')[1]
                try:
                    trigger = v[2]['custom_environment'][0].split('=')[1]
                except (IndexError, KeyError):
                    trigger = None

                try:
                    self.db.execute('INSERT INTO test (release, arch, package) '
                                    'VALUES (?, ?, ?)',
                                    (series, arch, source))
                except sqlite3.IntegrityError:
                    # Completely normal if we have more than one result for
                    # the same source package; ignore
                    pass

                self.db.execute('INSERT INTO result '
                                '(test_id, run_id, version, triggers, '
                                ' exitcode) '
                                'SELECT test.id, ?, ?, ?, ? FROM test '
                                'WHERE release=? AND arch=? AND package=?',
                                (latest, source_ver, trigger, retcode,
                                 series, arch, source))

        self.db.commit()

    def run_it(self, unstable_add, expect_status, expect_excuses={}):
        '''Run britney with some unstable packages and verify excuses.

        unstable_add is a list of (binpkgname, field_dict, testsuite_value)
        passed to TestData.add for "unstable".

        expect_status is a dict sourcename → (is_candidate, testsrc → arch → status)
        that is checked against the excuses YAML.

        expect_excuses is a dict sourcename →  [(key, value), ...]
        matches that are checked against the excuses YAML.

        Return (output, excuses_dict, excuses_html).
        '''
        for (pkg, fields, testsuite) in unstable_add:
            self.data.add(pkg, True, fields, True, testsuite)
            self.sourceppa_cache.setdefault(pkg, {})
            if fields['Version'] not in self.sourceppa_cache[pkg]:
                self.sourceppa_cache[pkg][fields['Version']] = ''
            self.email_cache.setdefault(pkg, {})
            self.email_cache[pkg][fields['Version']] = True

        # Set up sourceppa cache for testing
        sourceppa_path = os.path.join(self.data.dirs[True], 'SourcePPA')
        with open(sourceppa_path, 'w', encoding='utf-8') as sourceppa:
            sourceppa.write(json.dumps(self.sourceppa_cache))

        email_path = os.path.join(self.data.dirs[True], 'EmailCache')
        with open(email_path, 'w', encoding='utf-8') as email:
            email.write(json.dumps(self.email_cache))

        self.swift.start()
        (excuses_yaml, excuses_html, out) = self.run_britney()
        self.swift.stop()

        # convert excuses to source indexed dict
        excuses_dict = {}
        for s in yaml.safe_load(excuses_yaml)['sources']:
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
                    self.assertEqual(excuses_dict[src]['policy_info']['autopkgtest'][testsrc][arch][0],
                                     status,
                                     excuses_dict[src]['policy_info']['autopkgtest'][testsrc])

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
                    # debci-series-amd64:darkgreen {"triggers": ["darkgreen/2"], "submit-time": "2020-01-16 09:47:12"}
                    # strip the submit time from the requests we're testing; it
                    # is only for info for people reading the queue
                    (queuepkg, data) = line.split(' ', 1)
                    data_json = json.loads(data)
                    del data_json["submit-time"]
                    self.amqp_requests.add("{} {}".format(queuepkg,
                                                          json.dumps(data_json)))
            os.unlink(self.fake_amqp)
        except IOError:
            pass

        try:
            with open(os.path.join(self.data.path, 'data/testing/state/autopkgtest-pending.json')) as f:
                self.pending_requests = json.load(f)
        except IOError:
            self.pending_requests = None

        self.assertNotIn('FIXME', out)

        return (out, excuses_dict, excuses_html)


class AT(TestAutopkgtestBase):
    ################################################################
    # Tests for generic packages
    ################################################################

    def test_fail_on_missing_database(self):
        '''Fails if autopkgtest.db is requested but not available'''

        os.unlink(self.db_path)

        self.data.add_default_packages(lightgreen=False)

        britney_failed = 0
        try:
            self.run_it(
                # uninstallable unstable version
                [('lightgreen', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1 (>= 2)'}, 'autopkgtest')],
                {'lightgreen': (False, {})},
                {'lightgreen': [('old-version', '1'), ('new-version', '1.1~beta'),
                                ('reason', 'depends'),
                                ('excuses', 'uninstallable on arch amd64, not running autopkgtest there')
                                ]
                 })[1]
        except AssertionError as e:
            britney_failed = 1

        self.assertEqual(britney_failed, 1, "DB missing but britney succeeded")

    def test_no_request_for_uninstallable(self):
        '''Does not request a test for an uninstallable package'''

        self.data.add_default_packages(lightgreen=False)

        exc = self.run_it(
            # uninstallable unstable version
            [('lightgreen', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1 (>= 2)'}, 'autopkgtest')],
            {'lightgreen': (False, {})},
            {'lightgreen': [('old-version', '1'), ('new-version', '1.1~beta'),
                            ('reason', 'depends'),
                            ('excuses', 'uninstallable on arch amd64, not running autopkgtest there')
                            ]
             })[1]
        # autopkgtest should not be triggered for uninstallable pkg
        self.assertEqual(exc['lightgreen']['policy_info']['autopkgtest'], {'verdict': 'PASS'})

        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

        with open(os.path.join(self.data.path, 'output', 'output.txt')) as f:
            upgrade_out = f.read()
        self.assertNotIn('accepted:', upgrade_out)

    def test_no_request_for_excluded_arch(self):
        '''
        Does not request a test on an architecture for which the package
        produces no binaries
        '''

        self.data.add_default_packages()
        self.sourceppa_cache['purple'] = {'2': ''}

        # The package has passed before on i386
        self.set_results({'autopkgtest-testing': {
            'testing/i386/p/purple/20150101_100000@': (0, 'purple 1', tr('purple/1')),
            'testing/amd64/p/purple/20150101_100000@': (0, 'purple 1', tr('purple/1')),
            'testing/amd64/p/purple/20200101_100000@': (0, 'purple 2', tr('purple/2')),
        }})

        exc = self.run_it(
            [('libpurple1', {'Source': 'purple', 'Version': '2', 'Architecture': 'amd64'},
             'autopkgtest')],
            {'purple': (True, {'purple/2': {'amd64': 'PASS'}})},
        )[1]

        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

        with open(os.path.join(self.data.path, 'output', 'output.txt')) as f:
            upgrade_out = f.read()
        self.assertIn('accepted: purple', upgrade_out)
        self.assertIn('SUCCESS (1/0)', upgrade_out)

    def test_no_wait_for_always_failed_test(self):
        '''We do not need to wait for results for tests which have always failed'''

        self.data.add_default_packages(darkgreen=False)

        # The package has failed before, and with a trigger too on amd64
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (4, 'green 1'),
            'testing/amd64/d/darkgreen/20150101_100000@': (4, 'green 1', tr('failedbefore/1')),
        }})

        exc = self.run_it(
            [('darkgreen', {'Version': '2'}, 'autopkgtest')],
            {'darkgreen': (True, {'darkgreen': {'i386': 'RUNNING-ALWAYSFAIL', 'amd64': 'RUNNING-ALWAYSFAIL'}})},
        )[1]

        # the test should still be triggered though
        self.assertEqual(exc['darkgreen']['policy_info']['autopkgtest'],
                         {'darkgreen': {
                             'amd64': ['RUNNING-ALWAYSFAIL',
                                       'https://autopkgtest.ubuntu.com/running',
                                       'https://autopkgtest.ubuntu.com/packages/d/darkgreen/testing/amd64',
                                       None,
                                       None],
                             'i386': ['RUNNING-ALWAYSFAIL',
                                      'https://autopkgtest.ubuntu.com/running',
                                      'https://autopkgtest.ubuntu.com/packages/d/darkgreen/testing/i386',
                                      None,
                                      None]},
                         'verdict': 'PASS'})

        self.assertEqual(self.pending_requests,
                         {'darkgreen/2': {'darkgreen': ['amd64', 'i386']}})

        self.assertEqual(
            self.amqp_requests,
            set(['debci-testing-amd64:darkgreen {"triggers": ["darkgreen/2"]}',
                 'debci-testing-i386:darkgreen {"triggers": ["darkgreen/2"]}']))

        with open(os.path.join(self.data.path, 'output', 'output.txt')) as f:
            upgrade_out = f.read()
        self.assertIn('accepted: darkgreen', upgrade_out)
        self.assertIn('SUCCESS (1/0)', upgrade_out)

    def test_dropped_test_not_run(self):
        '''New version of a package drops its autopkgtest'''

        self.data.add_default_packages(green=False)

        # green has passed on amd64 before
        # lightgreen has passed on i386, therefore we should block on it returning
        self.set_results({'autopkgtest-testing': {
            'testing/amd64/g/green/20150101_100000@': (0, 'green 4', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
        }})

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green'}, None)],
            {'green': (False, {'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'}})
             },
            {'green': [('old-version', '1'), ('new-version', '2'),
                       ('reason', 'autopkgtest')]})

        # we expect the package's reverse dependencies' tests to get triggered,
        # but *not* the package itself since it has no autopkgtest any more
        self.assertEqual(
            self.amqp_requests,
            set(['debci-testing-i386:lightgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:lightgreen {"triggers": ["green/2"]}',
                 'debci-testing-i386:darkgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:darkgreen {"triggers": ["green/2"]}']))

        # ... and that they get recorded as pending
        expected_pending = {'green/2': {'darkgreen': ['amd64', 'i386'],
                                        'lightgreen': ['amd64', 'i386']}}
        self.assertEqual(self.pending_requests, expected_pending)

    def test_multi_rdepends_with_tests_all_running(self):
        '''Multiple reverse dependencies with tests (all running)'''

        self.data.add_default_packages(green=False)

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               })
             },
            {'green': [('old-version', '1'), ('new-version', '2'),
                       ('reason', 'autopkgtest')]})

        # we expect the package's and its reverse dependencies' tests to get
        # triggered
        self.assertEqual(
            self.amqp_requests,
            set(['debci-testing-i386:green {"triggers": ["green/2"]}',
                 'debci-testing-amd64:green {"triggers": ["green/2"]}',
                 'debci-testing-i386:lightgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:lightgreen {"triggers": ["green/2"]}',
                 'debci-testing-i386:darkgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:darkgreen {"triggers": ["green/2"]}']))

        # ... and that they get recorded as pending
        expected_pending = {'green/2': {'darkgreen': ['amd64', 'i386'],
                                        'green': ['amd64', 'i386'],
                                        'lightgreen': ['amd64', 'i386']}}
        self.assertEqual(self.pending_requests, expected_pending)

        # if we run britney again this should *not* trigger any new tests
        self.run_it([], {'green': (False, {})})
        self.assertEqual(self.amqp_requests, set())
        # but the set of pending tests doesn't change
        self.assertEqual(self.pending_requests, expected_pending)

    def test_multi_rdepends_with_tests_all_pass(self):
        '''Multiple reverse dependencies with tests (all pass)'''

        self.data.add_default_packages(green=False)

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        # first run requests tests and marks them as pending
        exc = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             # a reverse dep that does not exist in testing should not be triggered
             ('brittle', {'Depends': 'libgreen1'}, 'autopkgtest')],
            {'green': (False, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               })
             },
            {'green': [('old-version', '1'), ('new-version', '2')]})[1]
        self.assertNotIn('brittle', exc['green']['policy_info']['autopkgtest'])

        # second run collects the results
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('green/2')),
            # version in testing fails
            'testing/i386/g/green/20150101_020000@': (4, 'green 1', tr('green/1')),
            'testing/amd64/g/green/20150101_020000@': (4, 'green 1', tr('green/1')),
            # version in unstable succeeds
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            # new "brittle" succeeds
            'testing/i386/b/brittle/20150101_100200@': (0, 'brittle 1', tr('brittle/1')),
            'testing/amd64/b/brittle/20150101_100201@': (0, 'brittle 1', tr('brittle/1')),
        }})

        out = self.run_it(
            [],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             'brittle': (True, {'brittle/1': {'amd64': 'PASS', 'i386': 'PASS'}})
             },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )[0]

        # all tests ran, there should be no more pending ones
        self.assertEqual(self.pending_requests, {})

        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

        # caches the results and triggers
        with open(os.path.join(self.data.path, 'data/testing/state/autopkgtest-results.cache')) as f:
            res = json.load(f)
        self.assertEqual(res['green/1']['green']['amd64'],
                         ['FAIL', '1', '20150101_020000@', 1420077600])
        self.assertEqual(set(res['green/2']), {'darkgreen', 'green', 'lightgreen'})
        self.assertEqual(res['green/2']['lightgreen']['i386'],
                         ['PASS', '1', '20150101_100100@', 1420106460])

        # third run should not trigger any new tests, should all be in the
        # cache
        self.set_results({})
        out = self.run_it(
            [],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              })
             })[0]
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_with_tests_mixed(self):
        '''Multiple reverse dependencies with tests (mixed results)'''

        self.data.add_default_packages(green=False)

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        # first run requests tests and marks them as pending
        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               })
             },
            {'green': [('old-version', '1'), ('new-version', '2')]})

        # second run collects the results
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100201@': (4, 'green 2', tr('green/2')),
            # unrelated results (wrong trigger), ignore this!
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('blue/1')),
        }})

        out = self.run_it(
            [],
            {'green': (False, {'green/2': {'amd64': 'ALWAYSFAIL', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'REGRESSION', 'i386': 'RUNNING'},
                               'darkgreen/1': {'amd64': 'RUNNING', 'i386': 'PASS'},
                               })
             })[0]

        self.assertIn('Update Excuses generation completed', out)
        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out)

        # there should be some pending ones
        self.assertEqual(self.pending_requests,
                         {'green/2': {'darkgreen': ['amd64'], 'lightgreen': ['i386']}})

    def test_results_without_triggers(self):
        '''Old results without recorded triggers'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1'),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'testing/i386/g/green/20150101_100100@': (0, 'green 1', tr('passedbefore/1')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2'),
            'testing/amd64/g/green/20150101_100201@': (4, 'green 2'),
        }})

        # none of the above results should be accepted
        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               })
             })

        # there should be some pending ones
        self.assertEqual(self.pending_requests,
                         {'green/2': {'lightgreen': ['amd64', 'i386'],
                                      'green': ['amd64', 'i386'],
                                      'darkgreen': ['amd64', 'i386']}})

    def test_multi_rdepends_with_tests_regression(self):
        '''Multiple reverse dependencies with tests (regression)'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/1')),
            'testing/amd64/g/green/20150101_100201@': (4, 'green 2', tr('green/2')),
        }})

        out, exc, _ = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/2': {'amd64': 'REGRESSION', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               })
             },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )

        # should have links to log and history, but no artifacts (as this is
        # not a PPA)
        self.assertEqual(exc['green']['policy_info']['autopkgtest']['lightgreen/1']['amd64'][:4],
                         ['REGRESSION',
                          'http://localhost:18085/autopkgtest-testing/testing/amd64/l/lightgreen/20150101_100101@/log.gz',
                          'https://autopkgtest.ubuntu.com/packages/l/lightgreen/testing/amd64',
                          None])

        # should have retry link for the regressions (not a stable URL, test
        # seaprately)
        link = urllib.parse.urlparse(exc['green']['policy_info']['autopkgtest']['lightgreen/1']['amd64'][4])
        self.assertEqual(link.netloc, 'autopkgtest.ubuntu.com')
        self.assertEqual(link.path, '/request.cgi')
        self.assertEqual(urllib.parse.parse_qs(link.query),
                         {'release': ['testing'], 'arch': ['amd64'],
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

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/1')),
            'testing/amd64/g/green/20150101_100201@': (4, 'green 2', tr('green/2')),
        }})

        out = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/2': {'amd64': 'REGRESSION', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               })
             },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )[0]

        self.assertEqual(self.pending_requests, {})
        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_with_tests_always_failed(self):
        '''Multiple reverse dependencies with tests (always failed)'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100200@': (4, 'green 2', tr('green/1')),
            'testing/amd64/g/green/20150101_100201@': (4, 'green 2', tr('green/2')),
        }})

        out = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green/2': {'amd64': 'ALWAYSFAIL', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'ALWAYSFAIL', 'i386': 'ALWAYSFAIL'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              })
             },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )[0]

        self.assertEqual(self.pending_requests, {})
        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_arch_specific(self):
        '''Multiple reverse dependencies with arch specific tests'''

        self.data.add_default_packages(green=False)

        # green has passed before on amd64, doesn't exist on i386
        self.set_results({'autopkgtest-testing': {
            'testing/amd64/g/green64/20150101_100000@': (0, 'green64 0.1', tr('passedbefore/1')),
        }})

        self.data.add('green64', False, {'Depends': 'libc6 (>= 0.9), libgreen1',
                                         'Architecture': 'amd64'},
                      testsuite='autopkgtest')

        # first run requests tests and marks them as pending
        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'green64': {'amd64': 'RUNNING'},
                               })
             })

        self.assertEqual(
            self.amqp_requests,
            set(['debci-testing-i386:green {"triggers": ["green/2"]}',
                 'debci-testing-amd64:green {"triggers": ["green/2"]}',
                 'debci-testing-i386:lightgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:lightgreen {"triggers": ["green/2"]}',
                 'debci-testing-i386:darkgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:darkgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:green64 {"triggers": ["green/2"]}']))

        self.assertEqual(self.pending_requests,
                         {'green/2': {'lightgreen': ['amd64', 'i386'],
                                      'darkgreen': ['amd64', 'i386'],
                                      'green64': ['amd64'],
                                      'green': ['amd64', 'i386']}})

        # second run collects the results
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('green/2')),
            # version in testing fails
            'testing/i386/g/green/20150101_020000@': (4, 'green 1', tr('green/1')),
            'testing/amd64/g/green/20150101_020000@': (4, 'green 1', tr('green/1')),
            # version in unstable succeeds
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            # only amd64 result for green64
            'testing/amd64/g/green64/20150101_100200@': (0, 'green64 1', tr('green/2')),
        }})

        out = self.run_it(
            [],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'green64/1': {'amd64': 'PASS'},
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

        self.data.add_default_packages(green=False)

        self.data.add_src('green', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        self.data.add('libgreen1', True, {'Source': 'green',
                                          'Depends': 'libc6 (>= 0.9)'},
                      testsuite='autopkgtest', add_src=False)
        self.data.add('green', True, {'Depends': 'libc6 (>= 0.9), libgreen1',
                                      'Conflicts': 'blue'},
                      testsuite='autopkgtest', add_src=False)

        exc = self.run_it(
            # uninstallable unstable version
            [],
            {'green': (False, {})},
            {'green': [('old-version', '1'), ('new-version', '2'),
                       ('missing-builds', ON_ALL_ARCHES),
                       ]
             })[1]
        # autopkgtest should not be triggered for unbuilt pkg
        self.assertEqual(exc['green']['policy_info']['autopkgtest'], {'verdict': 'REJECTED_TEMPORARILY'})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_unbuilt_not_in_testing(self):
        '''Unbuilt package should not trigger tests or get considered (package not in testing)'''

        self.data.add_default_packages(green=False)

        self.sourceppa_cache['lime'] = {'1': ''}

        self.data.add_src('lime', True, {'Version': '1', 'Testsuite': 'autopkgtest'})
        exc = self.run_it(
            # unbuilt unstable version
            [],
            {'lime': (False, {})},
            {'lime': [('old-version', '-'), ('new-version', '1'),
                      ('reason', 'no-binaries'),
                      ]
             })[1]
        # autopkgtest should not be triggered for unbuilt pkg
        self.assertEqual(exc['lime']['policy_info']['autopkgtest'], {'verdict': 'REJECTED_TEMPORARILY'})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_partial_unbuilt(self):
        '''Unbuilt package on some arches should not trigger tests on those arches'''

        self.data.add_default_packages(green=False)

        self.data.add_src('green', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        self.data.add('libgreen1', True, {'Version': '2', 'Source': 'green', 'Architecture': 'i386'}, add_src=False)
        self.data.add('green', True, {'Depends': 'libc6 (>= 0.9), libgreen1',
                                      'Conflicts': 'blue'},
                      testsuite='autopkgtest', add_src=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
        }})

        exc = self.run_it(
            [],
            {'green': (False, {})},
            {'green': [('old-version', '1'), ('new-version', '2'),
                       ('missing-builds', {'on-architectures': ['amd64', 'arm64', 'armhf', 'powerpc', 'ppc64el'],
                                           'on-unimportant-architectures': []})
                       ]
             })[1]
        # autopkgtest should not be triggered on arches with unbuilt pkg
        self.assertEqual(exc['green']['policy_info']['autopkgtest']['verdict'], 'REJECTED_TEMPORARILY')
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_partial_unbuilt_block(self):
        '''Unbuilt blocked package on some arches should not trigger tests on those arches'''

        self.data.add_default_packages(green=False)

        self.create_hint('freeze', 'block-all source')

        self.data.add_src('green', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        self.data.add('libgreen1', True, {'Version': '2', 'Source': 'green', 'Architecture': 'i386'}, add_src=False)
        self.data.add('green', True, {'Depends': 'libc6 (>= 0.9), libgreen1',
                                      'Conflicts': 'blue'},
                      testsuite='autopkgtest', add_src=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
        }})

        exc = self.run_it(
            [],
            {'green': (False, {})},
            {'green': [('old-version', '1'), ('new-version', '2'),
                       ('missing-builds', {'on-architectures': ['amd64', 'arm64', 'armhf', 'powerpc', 'ppc64el'],
                                           'on-unimportant-architectures': []})
                       ]
             })[1]
        # autopkgtest should not be triggered on arches with unbuilt pkg
        self.assertEqual(exc['green']['policy_info']['autopkgtest']['verdict'], 'REJECTED_TEMPORARILY')
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_rdepends_unbuilt(self):
        '''Unbuilt reverse dependency'''

        self.data.add_default_packages(green=False, lightgreen=False)

        # old lightgreen fails, thus new green should be held back
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/1.1')),
            'testing/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/1.1')),
            'testing/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1.1')),
            'testing/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1.1')),
            'testing/i386/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'testing/amd64/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 1.1', tr('green/1.1')),
            'testing/amd64/g/green/20150101_100201@': (0, 'green 1.1', tr('green/1.1')),
        }})

        # add unbuilt lightgreen; should run tests against the old version
        self.data.add_src('lightgreen', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        self.data.add('lightgreen', True, {'Depends': 'libgreen1'},
                      testsuite='autopkgtest', add_src=False)

        self.run_it(
            [('libgreen1', {'Version': '1.1', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/1.1': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               }),
             'lightgreen': (False, {}),
             },
            {'green': [('old-version', '1'), ('new-version', '1.1')],
             'lightgreen': [('old-version', '1'), ('new-version', '2'),
                            ('missing-builds', ON_ALL_ARCHES)],
             }
        )

        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # next run should not trigger any new requests
        self.run_it([], {'green': (False, {}), 'lightgreen': (False, {})})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # now lightgreen 2 gets built, should trigger a new test run
        self.data.remove_all(True)
        self.data.add('libc6', True)
        self.data.add('darkgreen', True, {'Depends': 'libgreen1'},
                      testsuite='autopkgtest-pkg-foo')

        self.data.add('blue', True, {'Depends': 'libc6 (>= 0.9)',
                                     'Conflicts': 'green'},
                      testsuite='specialtest')
        self.data.add('black', True, {},
                      testsuite='autopkgtest')
        self.data.add('grey', True, {},
                      testsuite='autopkgtest')

        self.run_it(
            [('libgreen1', {'Version': '1.1', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2'}, 'autopkgtest')],
            {})
        self.assertEqual(self.amqp_requests,
                         set(['debci-testing-amd64:lightgreen {"triggers": ["lightgreen/2"]}',
                              'debci-testing-i386:lightgreen {"triggers": ["lightgreen/2"]}']))

        # next run collects the results
        self.set_results({'autopkgtest-testing': {
            'testing/i386/l/lightgreen/20150101_100200@': (0, 'lightgreen 2', tr('lightgreen/2')),
            'testing/amd64/l/lightgreen/20150101_102000@': (0, 'lightgreen 2', tr('lightgreen/2')),
        }})
        self.run_it(
            [],
            # green hasn't changed, the above re-run was for trigger lightgreen/2
            {'green': (False, {'green/1.1': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               }),
             'lightgreen': (True, {'lightgreen/2': {'amd64': 'PASS', 'i386': 'PASS'}}),
             },
            {'green': [('old-version', '1'), ('new-version', '1.1')],
             'lightgreen': [('old-version', '1'), ('new-version', '2')],
             }
        )
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_rdepends_unbuilt_unstable_only(self):
        '''Unbuilt reverse dependency which is not in testing'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'testing/amd64/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
        }})
        # run britney once to pick up previous results
        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'}})})

        # add new uninstallable brokengreen; should not run test at all
        exc = self.run_it(
            [('brokengreen', {'Version': '1', 'Depends': 'libgreen1, nonexisting'}, 'autopkgtest')],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'}}),
             'brokengreen': (False, {}),
             },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'brokengreen': [('old-version', '-'), ('new-version', '1'),
                             ('reason', 'depends'),
                             ('excuses', 'uninstallable on arch amd64, not running autopkgtest there')],
             })[1]
        # autopkgtest should not be triggered for uninstallable pkg
        self.assertEqual(self.amqp_requests, set())

    def test_rdepends_unbuilt_new_version_result(self):
        '''Unbuilt reverse dependency gets test result for newer version

        This might happen if the autopkgtest infrastructure runs the unstable
        source tests against the testing binaries. Even if that gets done
        properly it might still happen that at the time of the britney run the
        package isn't built yet, but it is once the test gets run.
        '''

        self.data.add_default_packages(green=False, lightgreen=False)

        # old lightgreen fails, thus new green should be held back
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/1.1')),
            'testing/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/1.1')),
            'testing/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1.1')),
            'testing/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('green/1.1')),
            'testing/i386/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'testing/amd64/g/green/20150101_020000@': (0, 'green 1', tr('green/1')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 1.1', tr('green/1.1')),
            'testing/amd64/g/green/20150101_100201@': (0, 'green 1.1', tr('green/1.1')),
        }})

        # add unbuilt lightgreen; should run tests against the old version
        self.data.add_src('lightgreen', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        self.data.add('lightgreen', True, {'Depends': 'libgreen1'},
                      testsuite='autopkgtest', add_src=False)

        self.run_it(
            [('libgreen1', {'Version': '1.1', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/1.1': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               }),
             'lightgreen': (False, {}),
             },
            {'green': [('old-version', '1'), ('new-version', '1.1')],
             'lightgreen': [('old-version', '1'), ('new-version', '2'),
                            ('missing-builds', ON_ALL_ARCHES)]
             }
        )
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # lightgreen 2 stays unbuilt in britney, but we get a test result for it
        self.set_results({'autopkgtest-testing': {
            'testing/i386/l/lightgreen/20150101_100200@': (0, 'lightgreen 2', tr('green/1.1')),
            'testing/amd64/l/lightgreen/20150101_102000@': (0, 'lightgreen 2', tr('green/1.1')),
        }})
        self.run_it(
            [],
            {'green': (True, {'green/1.1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             'lightgreen': (False, {}),
             },
            {'green': [('old-version', '1'), ('new-version', '1.1')],
             'lightgreen': [('old-version', '1'), ('new-version', '2'),
                            ('missing-builds', ON_ALL_ARCHES)]
             }
        )
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # next run should not trigger any new requests
        self.run_it([], {'green': (True, {}), 'lightgreen': (False, {})})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_rdepends_unbuilt_new_version_fail(self):
        '''Unbuilt reverse dependency gets failure for newer version'''

        self.data.add_default_packages(green=False, lightgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('lightgreen/1')),
        }})

        # add unbuilt lightgreen; should request tests against the old version
        self.data.add_src('lightgreen', True, {'Version': '2', 'Testsuite': 'autopkgtest'})
        self.data.add('lightgreen', True, {'Depends': 'libgreen1'},
                      testsuite='autopkgtest', add_src=False)

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               }),
             'lightgreen': (False, {}),
             },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '2'),
                            ('missing-builds', ON_ALL_ARCHES)],
             }
        )
        self.assertEqual(len(self.amqp_requests), 6)

        # we only get a result for lightgreen 2, not for the requested 1
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 0.5', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 0.5', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100200@': (4, 'lightgreen 2', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100200@': (4, 'lightgreen 2', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
        }})
        self.run_it(
            [],
            {'green': (False, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen/2': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               }),
             'lightgreen': (False, {}),
             },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '2'),
                            ('missing-builds', ON_ALL_ARCHES)],
             }
        )
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # next run should not trigger any new requests
        self.run_it([], {'green': (False, {}), 'lightgreen': (False, {})})
        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

# #    def test_same_version_binary_in_unstable(self):
# #        '''binary from new architecture in unstable with testing version'''
# #
# #        # Invalid dataset in Debian and Ubuntu: ... ARCHITECTURE all != i386
# #        self.data.add('lightgreen', False)
# #
# #        # i386 is in testing already, but amd64 just recently built and is in unstable
# #        self.data.add_src('brown', False, {'Testsuite': 'autopkgtest'})
# #        self.data.add_src('brown', True, {'Testsuite': 'autopkgtest'})
# #        self.data.add('brown', False, {'Architecture': 'i386'}, add_src=False)
# #        self.data.add('brown', True, {}, add_src=False)
# #
# #        exc = self.run_it(
# #            # we need some other package to create unstable Sources
# #            [('lightgreen', {'Version': '2'}, 'autopkgtest')],
# #            {'brown': (True, {})}
# #            )[1]
# #        self.assertEqual(exc['brown']['item-name'], 'brown/amd64')

    def test_package_pair_running(self):
        '''Two packages in unstable that need to go in together (running)'''

        self.data.add_default_packages(green=False, lightgreen=False)

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 2)'}, 'autopkgtest')],
            {'green': (False, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               }),
             'lightgreen': (False, {'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'}}),
             },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '2')],
             })

        # we expect the package's and its reverse dependencies' tests to get
        # triggered; lightgreen should be triggered for each trigger
        self.assertEqual(
            self.amqp_requests,
            set(['debci-testing-i386:green {"triggers": ["green/2"]}',
                 'debci-testing-amd64:green {"triggers": ["green/2"]}',
                 'debci-testing-i386:lightgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:lightgreen {"triggers": ["green/2"]}',
                 'debci-testing-i386:lightgreen {"triggers": ["lightgreen/2 green/2"]}',
                 'debci-testing-amd64:lightgreen {"triggers": ["lightgreen/2 green/2"]}',
                 'debci-testing-i386:darkgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:darkgreen {"triggers": ["green/2"]}']))

        # ... and that they get recorded as pending
        self.assertEqual(self.pending_requests,
                         {'lightgreen/2': {'lightgreen': ['amd64', 'i386']},
                          'green/2': {'darkgreen': ['amd64', 'i386'],
                                      'green': ['amd64', 'i386'],
                                      'lightgreen': ['amd64', 'i386']}})

    def test_binary_from_new_source_package_running(self):
        '''building an existing binary for a new source package (running)'''

        self.data.add_default_packages(green=False)

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'newgreen': (True, {'newgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                 'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                 'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                 }),
             },
            {'newgreen': [('old-version', '-'), ('new-version', '2')]})

        self.assertEqual(len(self.amqp_requests), 8)
        self.assertEqual(self.pending_requests,
                         {'newgreen/2': {'darkgreen': ['amd64', 'i386'],
                                         'green': ['amd64', 'i386'],
                                         'lightgreen': ['amd64', 'i386'],
                                         'newgreen': ['amd64', 'i386']}})

    def test_blacklisted_fail(self):
        '''blacklisted packages return exit code 99 and version blacklisted,
        check they are handled correctly'''

        self.data.add_default_packages(black=False, grey=False)
        self.data.add('brown', False, {'Depends': 'grey'}, testsuite='autopkgtest')
        self.data.add('brown', True, {'Depends': 'grey'}, testsuite='autopkgtest')

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/b/black/20150101_100000@': (0, 'black 1', tr('black/1')),
            'testing/amd64/b/black/20150102_100000@': (99, 'black blacklisted', tr('black/2')),
            'testing/amd64/g/grey/20150101_100000@': (99, 'grey blacklisted', tr('grey/1')),
            'testing/amd64/b/brown/20150101_100000@': (99, 'brown blacklisted', tr('grey/2')),
        }})

        self.run_it(
            [('black', {'Version': '2'}, 'autopkgtest'),
             ('grey', {'Version': '2'}, 'autopkgtest')],
            {'black': (False, {'black/blacklisted': {'amd64': 'REGRESSION'},
                               'black': {'i386': 'RUNNING-ALWAYSFAIL'}}),
             'grey': (True, {'grey': {'amd64': 'RUNNING-ALWAYSFAIL'},
                             'brown/blacklisted': {'amd64': 'ALWAYSFAIL'},
                             'brown': {'i386': 'RUNNING-ALWAYSFAIL'}})
             })

        self.assertEqual(len(self.amqp_requests), 4)
        self.assertEqual(self.pending_requests,
                         {'black/2': {'black': ['i386']},
                          'grey/2': {'grey': ['amd64', 'i386'],
                                     'brown': ['i386']}})

    def test_blacklisted_force(self):
        '''blacklisted packages return exit code 99 and version blacklisted,
        check they can be forced over'''

        self.data.add_default_packages(black=False)

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/b/black/20150101_100000@': (0, 'black 1', tr('black/1')),
            'testing/amd64/b/black/20150102_100000@': (99, 'black blacklisted', tr('black/2')),
            'testing/i386/b/black/20150101_100000@': (0, 'black 1', tr('black/1')),
            'testing/i386/b/black/20150102_100000@': (99, 'black blacklisted', tr('black/2')),
        }})

        self.create_hint('autopkgtest', 'force-badtest black/blacklisted')

        self.run_it(
            [('black', {'Version': '2'}, 'autopkgtest')],
            {'black': (True, {'black/blacklisted': {'amd64': 'IGNORE-FAIL',
                                                    'i386': 'IGNORE-FAIL'}})
             },
            {'black': [('old-version', '1'), ('new-version', '2')]})

        self.assertEqual(len(self.amqp_requests), 0)

    def test_blacklisted_force_mismatch(self):
        '''forcing a blacklisted package doesn't mean you force other versions'''

        self.data.add_default_packages(black=False)

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/b/black/20150101_100000@': (0, 'black 1', tr('black/1')),
            'testing/i386/b/black/20150101_100001@': (0, 'black 1', tr('black/1')),
            'testing/amd64/b/black/20150102_100000@': (4, 'black 2', tr('black/2')),
            'testing/i386/b/black/20150102_100001@': (4, 'black 2', tr('black/2'))
        }})

        self.create_hint('autopkgtest', 'force-badtest black/amd64/blacklisted')

        self.run_it(
            [('black', {'Version': '2'}, 'autopkgtest')],
            {'black': (False, {'black/2': {'amd64': 'REGRESSION'}})
            },
            {'black': [('old-version', '1'), ('new-version', '2')]})

        self.assertEqual(len(self.amqp_requests), 0)

    def test_binary_from_new_source_package_pass(self):
        '''building an existing binary for a new source package (pass)'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('newgreen/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('newgreen/2')),
            'testing/i386/g/green/20150101_100000@': (0, 'green 1', tr('newgreen/2')),
            'testing/amd64/g/green/20150101_100000@': (0, 'green 1', tr('newgreen/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('newgreen/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('newgreen/2')),
            'testing/i386/n/newgreen/20150101_100200@': (0, 'newgreen 2', tr('newgreen/2')),
            'testing/amd64/n/newgreen/20150101_100201@': (0, 'newgreen 2', tr('newgreen/2')),
        }})

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'newgreen': (True, {'newgreen/2': {'amd64': 'PASS', 'i386': 'PASS'},
                                 'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                                 'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                                 'green/1': {'amd64': 'PASS', 'i386': 'PASS'},
                                 }),
             },
            {'newgreen': [('old-version', '-'), ('new-version', '2')]})

        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_result_from_older_version(self):
        '''test result from older version than the uploaded one'''

        self.data.add_default_packages(darkgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('darkgreen/1')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('darkgreen/1')),
        }})

        self.run_it(
            [('darkgreen', {'Version': '2', 'Depends': 'libc6 (>= 0.9), libgreen1'}, 'autopkgtest')],
            {'darkgreen': (False, {'darkgreen': {'amd64': 'RUNNING', 'i386': 'RUNNING'}})})

        self.assertEqual(
            self.amqp_requests,
            set(['debci-testing-i386:darkgreen {"triggers": ["darkgreen/2"]}',
                 'debci-testing-amd64:darkgreen {"triggers": ["darkgreen/2"]}']))
        self.assertEqual(self.pending_requests,
                         {'darkgreen/2': {'darkgreen': ['amd64', 'i386']}})

        # second run gets the results for darkgreen 2
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100010@': (0, 'darkgreen 2', tr('darkgreen/2')),
            'testing/amd64/d/darkgreen/20150101_100010@': (0, 'darkgreen 2', tr('darkgreen/2')),
        }})
        self.run_it(
            [],
            {'darkgreen': (True, {'darkgreen/2': {'amd64': 'PASS', 'i386': 'PASS'}})})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # next run sees a newer darkgreen, should re-run tests
        self.data.remove_all(True)
        self.data.add('libc6', True)
        self.data.add('libgreen1', True, {'Source': 'green',
                                          'Depends': 'libc6 (>= 0.9)'},
                      testsuite='autopkgtest')
        self.data.add('green', True, {'Depends': 'libc6 (>= 0.9), libgreen1',
                                      'Conflicts': 'blue'},
                      testsuite='autopkgtest')
        self.data.add('lightgreen', True, {'Depends': 'libgreen1'},
                      testsuite='autopkgtest')
        self.data.add('blue', True, {'Depends': 'libc6 (>= 0.9)',
                                     'Conflicts': 'green'},
                      testsuite='specialtest')
        self.data.add('black', True, {},
                      testsuite='autopkgtest')
        self.data.add('grey', True, {},
                      testsuite='autopkgtest')

        self.run_it(
            [('darkgreen', {'Version': '3', 'Depends': 'libc6 (>= 0.9), libgreen1'}, 'autopkgtest')],
            {'darkgreen': (False, {'darkgreen': {'amd64': 'RUNNING', 'i386': 'RUNNING'}})})
        self.assertEqual(
            self.amqp_requests,
            set(['debci-testing-i386:darkgreen {"triggers": ["darkgreen/3"]}',
                 'debci-testing-amd64:darkgreen {"triggers": ["darkgreen/3"]}']))
        self.assertEqual(self.pending_requests,
                         {'darkgreen/3': {'darkgreen': ['amd64', 'i386']}})

    def test_old_result_from_rdep_version(self):
        '''re-runs reverse dependency test on new versions'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100000@': (0, 'green 1', tr('green/1')),
            'testing/amd64/g/green/20150101_100000@': (0, 'green 1', tr('green/1')),
            'testing/i386/g/green/20150101_100010@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100010@': (0, 'green 2', tr('green/2')),
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/2')),
        }})

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             })

        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})
        self.data.remove_all(True)

        # second run: new version re-triggers all tests
        self.run_it(
            [('libgreen1', {'Version': '3', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'lightgreen': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               'darkgreen': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               }),
             })

        self.assertEqual(len(self.amqp_requests), 6)
        self.assertEqual(self.pending_requests,
                         {'green/3': {'darkgreen': ['amd64', 'i386'],
                                      'green': ['amd64', 'i386'],
                                      'lightgreen': ['amd64', 'i386']}})

        # third run gets the results for green and lightgreen, darkgreen is
        # still running
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100020@': (0, 'green 3', tr('green/3')),
            'testing/amd64/g/green/20150101_100020@': (0, 'green 3', tr('green/3')),
            'testing/i386/l/lightgreen/20150101_100010@': (0, 'lightgreen 1', tr('green/3')),
            'testing/amd64/l/lightgreen/20150101_100010@': (0, 'lightgreen 1', tr('green/3')),
        }})
        self.run_it(
            [],
            {'green': (False, {'green/3': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               'darkgreen': {'amd64': 'RUNNING', 'i386': 'RUNNING'},
                               }),
             })
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests,
                         {'green/3': {'darkgreen': ['amd64', 'i386']}})

        # fourth run finally gets the new darkgreen result
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100010@': (0, 'darkgreen 1', tr('green/3')),
            'testing/amd64/d/darkgreen/20150101_100010@': (0, 'darkgreen 1', tr('green/3')),
        }})
        self.run_it(
            [],
            {'green': (True, {'green/3': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             })
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_different_versions_on_arches(self):
        '''different tested package versions on different architectures'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('passedbefore/1')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('passedbefore/1')),
        }})

        # first run: no results yet
        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green'}, 'autopkgtest')],
            {'green': (False, {'darkgreen': {'amd64': 'RUNNING', 'i386': 'RUNNING'}})})

        # second run: i386 result has version 1.1
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100010@': (0, 'darkgreen 1.1', tr('green/2'))
        }})
        self.run_it(
            [],
            {'green': (False, {'darkgreen': {'amd64': 'RUNNING'},
                               'darkgreen/1.1': {'i386': 'PASS'},
                               })})

        # third run: amd64 result has version 1.2
        self.set_results({'autopkgtest-testing': {
            'testing/amd64/d/darkgreen/20150101_100010@': (0, 'darkgreen 1.2', tr('green/2')),
        }})
        self.run_it(
            [],
            {'green': (True, {'darkgreen/1.2': {'amd64': 'PASS'},
                              'darkgreen/1.1': {'i386': 'PASS'},
                              })})

    def test_tmpfail(self):
        '''tmpfail results'''

        self.data.add_default_packages(lightgreen=False)

        # one tmpfail result without testpkg-version, should be ignored
        self.set_results({'autopkgtest-testing': {
            'testing/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'testing/i386/l/lightgreen/20150101_100101@': (16, None, tr('lightgreen/2')),
            'testing/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (16, 'lightgreen 2', tr('lightgreen/2')),
        }})

        self.run_it(
            [('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 1)'}, 'autopkgtest')],
            {'lightgreen': (False, {'lightgreen/2': {'amd64': 'REGRESSION', 'i386': 'RUNNING'}})})
        self.assertEqual(self.pending_requests,
                         {'lightgreen/2': {'lightgreen': ['i386']}})

        # one more tmpfail result, should not confuse britney with None version
        self.set_results({'autopkgtest-testing': {
            'testing/i386/l/lightgreen/20150101_100201@': (16, None, tr('lightgreen/2')),
        }})
        self.run_it(
            [],
            {'lightgreen': (False, {'lightgreen/2': {'amd64': 'REGRESSION', 'i386': 'RUNNING'}})})
        with open(os.path.join(self.data.path, 'data/testing/state/autopkgtest-results.cache')) as f:
            contents = f.read()
        self.assertNotIn('null', contents)
        self.assertNotIn('None', contents)

    def test_rerun_failure(self):
        '''manually re-running failed tests gets picked up'''

        self.data.add_default_packages(green=False)

        # first run fails
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100000@': (0, 'green 2', tr('green/1')),
            'testing/i386/g/green/20150101_100101@': (4, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100000@': (0, 'green 2', tr('green/1')),
            'testing/amd64/g/green/20150101_100101@': (4, 'green 2', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
        }})

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/2': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'lightgreen/1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               }),
             })
        self.assertEqual(self.pending_requests, {})

        # re-running test manually succeeded (note: darkgreen result should be
        # cached already)
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100201@': (0, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100201@': (0, 'lightgreen 1', tr('green/2')),
        }})
        self.run_it(
            [],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
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

        self.data.add_default_packages(libc6=False)

        # new libc6 works fine with green
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100000@': (0, 'green 1', tr('libc6/2')),
            'testing/amd64/g/green/20150101_100000@': (0, 'green 1', tr('libc6/2')),
        }})

        self.run_it(
            [('libc6', {'Version': '2'}, None)],
            {'libc6': (True, {'green/1': {'amd64': 'PASS', 'i386': 'PASS'}})})
        self.assertEqual(self.pending_requests, {})

        self.data.remove_all(True)
        self.data.add('libc6', True, {'Version': '2'})
        self.data.add('lightgreen', True, {'Depends': 'libgreen1'},
                      testsuite='autopkgtest')
        self.data.add('blue', True, {'Depends': 'libc6 (>= 0.9)',
                                     'Conflicts': 'green'},
                      testsuite='specialtest')
        self.data.add('black', True, {},
                      testsuite='autopkgtest')
        self.data.add('grey', True, {},
                      testsuite='autopkgtest')

        # new green fails; that's not libc6's fault though, so it should stay
        # valid
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100100@': (4, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100100@': (4, 'green 2', tr('green/2')),
        }})
        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/2': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'}}),
             'libc6': (True, {'green/1': {'amd64': 'PASS', 'i386': 'PASS'}}),
             })
        self.assertEqual(
            self.amqp_requests,
            set(['debci-testing-i386:darkgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:darkgreen {"triggers": ["green/2"]}',
                 'debci-testing-i386:lightgreen {"triggers": ["green/2"]}',
                 'debci-testing-amd64:lightgreen {"triggers": ["green/2"]}',
                 ]))

    def test_remove_from_unstable(self):
        '''broken package gets removed from unstable'''

        self.data.add_default_packages(green=False, lightgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100101@': (0, 'green 1', tr('green/1')),
            'testing/amd64/g/green/20150101_100101@': (0, 'green 1', tr('green/1')),
            'testing/i386/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100201@': (4, 'lightgreen 2', tr('green/2 lightgreen/2')),
            'testing/amd64/l/lightgreen/20150101_100201@': (4, 'lightgreen 2', tr('green/2 lightgreen/2')),
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100001@': (0, 'darkgreen 1', tr('green/2')),
        }})

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 2)'}, 'autopkgtest')],
            {'green': (False, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen/2': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               }),
             })
        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

        # remove new lightgreen by resetting archive indexes, and re-adding
        # green
        self.data.remove_all(True)

        self.set_results({'autopkgtest-testing': {
            # add new result for lightgreen 1
            'testing/i386/l/lightgreen/20150101_100301@': (0, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100301@': (0, 'lightgreen 1', tr('green/2')),
        }})

        # next run should re-trigger lightgreen 1 to test against green/2
        exc = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             })[1]
        self.assertNotIn('lightgreen 2', exc['green']['policy_info']['autopkgtest'])

        # should not trigger new requests
        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

        # but the next run should not trigger anything new
        self.run_it(
            [],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             })
        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

# #    def test_multiarch_dep(self):
# #        '''multi-arch dependency'''
# #        # needs changes in britney2/installability/builder.py
# #
# #        self.data.add_default_packages(lightgreen=False)
# #
# #        # lightgreen has passed before on i386 only, therefore ALWAYSFAIL on amd64
# #        self.set_results({'autopkgtest-testing': {
# #            'testing/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('passedbefore/1')),
# #        }})
# #
# #        self.data.add('rainbow', False, {'Depends': 'lightgreen:any'},
# #                      testsuite='autopkgtest')
# #        self.data.add('rainbow', True, {'Depends': 'lightgreen:any'},
# #                      testsuite='autopkgtest')
# #
# #        self.run_it(
# #            [('lightgreen', {'Version': '2'}, 'autopkgtest')],
# #            {'lightgreen': (False, {'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
# #                                    'rainbow': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
# #                                   }),
# #            },
# #            {'lightgreen': [('old-version', '1'), ('new-version', '2')]}
# #        )

    def test_nbs(self):
        '''source-less binaries do not cause harm'''

        self.data.add_default_packages(green=False)

        # NBS in testing
        self.data.add('liboldgreen0', False, add_src=False)
        # NBS in unstable
        self.data.add('liboldgreen1', True, add_src=False)
        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green'}, 'autopkgtest')],
            {'green': (True, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              }),
             },
            {'green': [('old-version', '1'), ('new-version', '2')]})

    def test_newer_version_in_testing(self):
        '''Testing version is newer than in unstable'''

        self.data.add_default_packages(lightgreen=False)

        exc = self.run_it(
            [('lightgreen', {'Version': '0.9~beta'}, 'autopkgtest')],
            {'lightgreen': (False, {})},
            {'lightgreen': [('old-version', '1'), ('new-version', '0.9~beta'),
                            ('reason', 'newerintesting'),
                            ('excuses', 'ALERT: lightgreen is newer in the target suite (1 0.9~beta)')
                            ]
             })[1]

        # autopkgtest should not be triggered
        self.assertNotIn('autopkgtest', exc['lightgreen'].get('policy_info', {}))
        self.assertEqual(self.pending_requests, {})
        self.assertEqual(self.amqp_requests, set())

    def test_testsuite_triggers(self):
        '''Testsuite-Triggers'''

        self.data.add_default_packages(lightgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/r/rainbow/20150101_100000@': (0, 'rainbow 1', tr('passedbefore/1')),
        }})

        self.data.add('rainbow', False, testsuite='autopkgtest',
                      srcfields={'Testsuite-Triggers': 'unicorn, lightgreen, sugar'})

        self.run_it(
            [('lightgreen', {'Version': '2'}, 'autopkgtest')],
            {'lightgreen': (False, {'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                    'rainbow': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                                    }),
             }
        )

    def test_huge_number_of_tests(self):
        '''package triggers huge number of tests'''

        self.data.add_default_packages(green=False)

        for i in range(30):
            self.data.add('green%i' % i, False, {'Depends': 'libgreen1'}, testsuite='autopkgtest')

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green'}, 'autopkgtest')],
            {'green': (True, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              'green0': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              'green29': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              })
             },
        )

        # requests should all go into the -huge queues
        self.assertEqual([x for x in self.amqp_requests if 'huge' not in x], [])
        for i in range(30):
            for arch in ['i386', 'amd64']:
                self.assertIn('debci-huge-testing-%s:green%i {"triggers": ["green/2"]}' %
                              (arch, i), self.amqp_requests)

    ################################################################
    # Tests for hint processing
    ################################################################

    def test_hint_force_badtest(self):
        '''force-badtest hint'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
        }})

        self.create_hint('autopkgtest', 'force-badtest lightgreen/1')

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'IGNORE-FAIL', 'i386': 'IGNORE-FAIL'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             },
            {'green': [('old-version', '1'), ('new-version', '2')]
             })

    def test_hint_force_badtest_multi_version(self):
        '''force-badtest hint'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 2', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
        }})

        self.create_hint('autopkgtest', 'force-badtest lightgreen/1')

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen/1': {'i386': 'IGNORE-FAIL'},
                               'lightgreen/2': {'amd64': 'REGRESSION'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               }),
             },
            {'green': [('old-version', '1'), ('new-version', '2')]
             })

        # hint the version on amd64 too
        self.create_hint('autopkgtest', 'force-badtest lightgreen/2')

        self.run_it(
            [],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'i386': 'IGNORE-FAIL'},
                              'lightgreen/2': {'amd64': 'IGNORE-FAIL'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             },
            {'green': [('old-version', '1'), ('new-version', '2')]
             })

    def test_hint_force_badtest_different_version(self):
        '''force-badtest hint with non-matching version'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
        }})

        # lower hint version should not apply
        self.create_hint('autopkgtest', 'force-badtest lightgreen/0.1')

        exc = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               }),
             },
            {'green': [('reason', 'autopkgtest')]}
        )[1]
        self.assertNotIn('forced-reason', exc['green'])

        # higher hint version should apply
        self.create_hint('autopkgtest', 'force-badtest lightgreen/3')
        self.run_it(
            [],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'IGNORE-FAIL', 'i386': 'IGNORE-FAIL'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             },
            {}
        )

    def test_hint_force_badtest_arch(self):
        '''force-badtest hint for architecture instead of version'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
        }})

        self.create_hint('autopkgtest', 'force-badtest lightgreen/amd64/all')

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'IGNORE-FAIL', 'i386': 'REGRESSION'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               }),
             },
            {'green': [('old-version', '1'), ('new-version', '2')]
             })

        # hint i386 too, then it should become valid
        self.create_hint('autopkgtest', 'force-badtest lightgreen/i386/all')

        self.run_it(
            [],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen/1': {'amd64': 'IGNORE-FAIL', 'i386': 'IGNORE-FAIL'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             },
            {'green': [('old-version', '1'), ('new-version', '2')]
             })

    def test_hint_force_badtest_running(self):
        '''force-badtest hint on running test'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
        }})

        self.create_hint('autopkgtest', 'force-badtest lightgreen/1')

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green/2': {'amd64': 'PASS', 'i386': 'PASS'},
                              'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             },
            {'green': [('old-version', '1'), ('new-version', '2')]
             })

    def test_hint_force_skiptest(self):
        '''force-skiptest hint'''

        self.data.add_default_packages(green=False)

        self.create_hint('autopkgtest', 'force-skiptest green/2')

        # regression of green, darkgreen ok, lightgreen running
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
            'testing/i386/g/green/20150101_100200@': (4, 'green 2', tr('green/2')),
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
        }})
        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {'green/2': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'REGRESSION'},
                              'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                              'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                              }),
             },
            {'green': [('old-version', '1'), ('new-version', '2'),
                       ('reason', 'skiptest'),
                       ('excuses', 'Should wait for tests relating to green 2, but forced by autopkgtest')]
             })

    def test_hint_force_skiptest_different_version(self):
        '''force-skiptest hint with non-matching version'''

        self.data.add_default_packages(green=False)

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        self.create_hint('autopkgtest', 'force-skiptest green/1')
        exc = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               }),
             },
            {'green': [('reason', 'autopkgtest')]}
        )[1]
        self.assertNotIn('forced-reason', exc['green'])

    def test_hint_blockall_runs_tests(self):
        '''block-all hint still runs tests'''

        self.data.add_default_packages(lightgreen=False)

        self.create_hint('freeze', 'block-all source')

        self.set_results({'autopkgtest-testing': {
            'testing/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('passedbefore/1')),
            'testing/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('passedbefore/1')),
        }})

        self.run_it(
            [('lightgreen', {'Version': '2'}, 'autopkgtest')],
            {'lightgreen': (False, {'lightgreen': {'amd64': 'RUNNING', 'i386': 'RUNNING'}})}
        )

        self.set_results({'autopkgtest-testing': {
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 2', tr('lightgreen/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 2', tr('lightgreen/2')),
        }})

        self.run_it(
            [],
            {'lightgreen': (False, {'lightgreen/2': {'amd64': 'PASS', 'i386': 'PASS'}})},
            {'lightgreen': [('reason', 'block')]}
        )

    def test_hint_force_reset_test_goodbad_alwaysfail(self):
        '''force-reset-test hint marks as alwaysfail'''

        self.data.add_default_packages(lightgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 2', tr('lightgreen/2')),
        }})

        self.create_hint('autopkgtest', 'force-reset-test lightgreen/1')

        self.run_it(
            [('lightgreen', {'Version': '2', 'Source': 'lightgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'lightgreen': (True, {
                              'lightgreen/2': {'amd64': 'ALWAYSFAIL'},
                             }),
            },
            {'lightgreen': [('old-version', '1'), ('new-version', '2')]
            })

    def test_hint_force_reset_test_goodbad_alwaysfail_arch(self):
        '''force-reset-test hint marks as alwaysfail per arch'''

        self.data.add_default_packages(lightgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 2', tr('lightgreen/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'testing/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 2', tr('lightgreen/2')),
        }})

        self.create_hint('autopkgtest', 'force-reset-test lightgreen/1/amd64')

        self.run_it(
            [('lightgreen', {'Version': '2', 'Source': 'lightgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'lightgreen': (False, {
                             'lightgreen/2': {'amd64': 'ALWAYSFAIL', 'i386': 'REGRESSION'},
                             }),
            },
            {'lightgreen': [('old-version', '1'), ('new-version', '2')]
            })

    def test_hint_force_reset_test_bad_good_pass(self):
        '''force-reset-test hint followed by pass is pass'''

        self.data.add_default_packages(lightgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('lightgreen/1')),
            'testing/amd64/l/lightgreen/20150102_100101@': (0, 'lightgreen 2', tr('lightgreen/2')),
        }})

        self.create_hint('autopkgtest', 'force-reset-test lightgreen/1')

        self.run_it(
            [('lightgreen', {'Version': '2', 'Source': 'lightgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'lightgreen': (True, {
                              'lightgreen/2': {'amd64': 'PASS'},
                             }),
            },
            {'lightgreen': [('old-version', '1'), ('new-version', '2')]
            })

    def test_hint_force_reset_test_bad_good_bad_regression(self):
        '''force-reset-test hint followed by good, bad is regression'''

        self.data.add_default_packages(lightgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 1', tr('lightgreen/1')),
            'testing/amd64/l/lightgreen/20150102_100101@': (0, 'lightgreen 2', tr('lightgreen/2')),
            'testing/amd64/l/lightgreen/20150103_100101@': (4, 'lightgreen 3', tr('lightgreen/3')),
        }})

        self.create_hint('autopkgtest', 'force-reset-test lightgreen/1')

        self.run_it(
            [('lightgreen', {'Version': '3', 'Source': 'lightgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'lightgreen': (False, {
                              'lightgreen/3': {'amd64': 'REGRESSION'},
                             }),
            },
            {'lightgreen': [('old-version', '1'), ('new-version', '3')]
            })

    def test_hint_force_reset_test_bad_good_bad_regression_different_trigger(self):
        '''force-reset-test hint followed by good, bad is regression (not self-triggered)'''

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/l/lightgreen/20150101_100100@': (4, 'lightgreen 0.1', tr('lightgreen/0.1')),
            'testing/amd64/l/lightgreen/20150102_100101@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'testing/amd64/l/lightgreen/20150103_100101@': (4, 'lightgreen 1', tr('green/2')),
        }})

        self.create_hint('autopkgtest', 'force-reset-test lightgreen/0.1')

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {
                              'lightgreen/1': {'amd64': 'REGRESSION'},
                             }),
            },
            {'green': [('old-version', '1'), ('new-version', '2')]
            })

    def test_hint_force_reset_test_multiple_hints(self):
        '''force-reset-test multiple hints check ranges'''

        self.data.add_default_packages(green=False, lightgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'testing/amd64/l/lightgreen/20150102_100100@': (4, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150103_100101@': (0, 'lightgreen 2', tr('lightgreen/2')),
            'testing/amd64/l/lightgreen/20150104_100101@': (4, 'lightgreen 3', tr('lightgreen/3')),
        }})

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '3', 'Source': 'lightgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {
                              'lightgreen/1': {'amd64': 'REGRESSION'},
                             }),
            },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '3')],
            })

        self.create_hint('autopkgtest', 'force-reset-test lightgreen/1')
        self.run_it(
            [],
            {'green': (True, {
                              'lightgreen/1': {'amd64': 'ALWAYSFAIL'},
                             }),
             'lightgreen': (False, {
                              'lightgreen/3': {'amd64': 'REGRESSION'},
                             }),

            },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '3')],
            })
        self.create_hint('autopkgtest', 'force-reset-test lightgreen/3')
        self.run_it(
            [],
            {'green': (True, {
                              'lightgreen/1': {'amd64': 'ALWAYSFAIL'},
                             }),
             'lightgreen': (True, {
                              'lightgreen/3': {'amd64': 'ALWAYSFAIL'},
                             }),

            },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '3')],
            })

    def test_hint_force_reset_test_earlier_hints(self):
        '''force-reset-test for a later version applies backwards'''

        self.data.add_default_packages(green=False, lightgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'testing/amd64/l/lightgreen/20150102_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150103_100102@': (0, 'lightgreen 2', tr('lightgreen/2')),
            'testing/amd64/l/lightgreen/20150104_100102@': (4, 'lightgreen 3', tr('lightgreen/3')),
        }})

        self.create_hint('autopkgtest', 'force-reset-test lightgreen/3')
        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '3', 'Source': 'lightgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {
                              'lightgreen/1': {'amd64': 'ALWAYSFAIL'},
                             }),
             'lightgreen': (True, {
                              'lightgreen/3': {'amd64': 'ALWAYSFAIL'},
                             }),

            },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '3')],
            })

    def test_hint_force_reset_test_earlier_hints_pass(self):
        '''force-reset-test for a later version which is PASS is still PASS'''

        self.data.add_default_packages(green=False, lightgreen=False)

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 1', tr('lightgreen/1')),
            'testing/amd64/l/lightgreen/20150102_100101@': (0, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150103_100102@': (0, 'lightgreen 2', tr('lightgreen/2')),
            'testing/amd64/l/lightgreen/20150104_100102@': (0, 'lightgreen 3', tr('lightgreen/3')),
        }})

        self.create_hint('autopkgtest', 'force-reset-test lightgreen/3')
        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '3', 'Source': 'lightgreen', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {
                              'lightgreen/1': {'amd64': 'PASS'},
                             }),
             'lightgreen': (True, {
                              'lightgreen/3': {'amd64': 'PASS'},
                             }),

            },
            {'green': [('old-version', '1'), ('new-version', '2')],
             'lightgreen': [('old-version', '1'), ('new-version', '3')],
            })

    ################################################################
    # Kernel related tests
    ################################################################

    def test_detect_dkms_autodep8(self):
        '''DKMS packages are autopkgtested (via autodep8)'''

        self.data.add('dkms', False, {})
        self.data.add('fancy-dkms', False, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'}, testsuite='autopkgtest-pkg-dkms')

        self.set_results({'autopkgtest-testing': {
            'testing/i386/f/fancy/20150101_100101@': (0, 'fancy 0.1', tr('passedbefore/1'))
        }})

        self.run_it(
            [('dkms', {'Version': '2'}, None)],
            {'dkms': (False, {'fancy': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'}})},
            {'dkms': [('old-version', '1'), ('new-version', '2')]})

    def test_kernel_triggers_dkms(self):
        '''DKMS packages get triggered by kernel uploads'''

        self.data.add('dkms', False, {})
        self.data.add('fancy-dkms', False, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'}, testsuite='autopkgtest-pkg-dkms')

        self.run_it(
            [('linux-image-generic', {'Source': 'linux-meta'}, None),
             ('linux-image-grumpy-generic', {'Source': 'linux-meta-lts-grumpy'}, None),
             ('linux-image-64only', {'Source': 'linux-meta-64only', 'Architecture': 'amd64'}, None),
             ],
            {'linux-meta': (True, {'fancy': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'}}),
             'linux-meta-lts-grumpy': (True, {'fancy': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'}}),
             'linux-meta-64only': (True, {'fancy': {'amd64': 'RUNNING-ALWAYSFAIL'}}),
             })

        # one separate test should be triggered for each kernel
        self.assertEqual(
            self.amqp_requests,
            set(['debci-testing-i386:fancy {"triggers": ["linux-meta/1"]}',
                 'debci-testing-amd64:fancy {"triggers": ["linux-meta/1"]}',
                 'debci-testing-i386:fancy {"triggers": ["linux-meta-lts-grumpy/1"]}',
                 'debci-testing-amd64:fancy {"triggers": ["linux-meta-lts-grumpy/1"]}',
                 'debci-testing-amd64:fancy {"triggers": ["linux-meta-64only/1"]}']))

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
        self.set_results({'autopkgtest-testing': {
            'testing/amd64/f/fancy/20150101_100301@': (0, 'fancy 0.5', tr('passedbefore/1')),
            'testing/i386/f/fancy/20150101_100101@': (0, 'fancy 1', tr('linux-meta/1')),
            'testing/amd64/f/fancy/20150101_100101@': (0, 'fancy 1', tr('linux-meta/1')),
            'testing/amd64/f/fancy/20150101_100201@': (0, 'fancy 1', tr('linux-meta-64only/1')),
            'testing/i386/f/fancy/20150101_100301@': (4, 'fancy 1', tr('linux-meta-lts-grumpy/1')),
        }})

        self.run_it(
            [('linux-image-generic', {'Source': 'linux-meta'}, None),
             ('linux-image-grumpy-generic', {'Source': 'linux-meta-lts-grumpy'}, None),
             ('linux-image-64only', {'Source': 'linux-meta-64only', 'Architecture': 'amd64'}, None),
             ],
            {'linux-meta': (True, {'fancy/1': {'amd64': 'PASS', 'i386': 'PASS'}}),
             'linux-meta-lts-grumpy': (False, {'fancy/1': {'amd64': 'RUNNING', 'i386': 'ALWAYSFAIL'}}),
             'linux-meta-64only': (True, {'fancy/1': {'amd64': 'PASS'}}),
             })

        self.assertEqual(self.pending_requests,
                         {'linux-meta-lts-grumpy/1': {'fancy': ['amd64']}})

    def test_dkms_results_per_kernel_old_results(self):
        '''DKMS results get mapped to the triggering kernel version, old results'''

        self.data.add('dkms', False, {})
        self.data.add('fancy-dkms', False, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'}, testsuite='autopkgtest-pkg-dkms')

        # works against linux-meta and -64only, fails against grumpy i386, no
        # result yet for grumpy amd64
        self.set_results({'autopkgtest-testing': {
            # old results without trigger info
            'testing/i386/f/fancy/20140101_100101@': (0, 'fancy 1', {}),
            'testing/amd64/f/fancy/20140101_100101@': (8, 'fancy 1', {}),
            # current results with triggers
            'testing/i386/f/fancy/20150101_100101@': (0, 'fancy 1', tr('linux-meta/1')),
            'testing/amd64/f/fancy/20150101_100101@': (0, 'fancy 1', tr('linux-meta/1')),
            'testing/amd64/f/fancy/20150101_100201@': (0, 'fancy 1', tr('linux-meta-64only/1')),
            'testing/i386/f/fancy/20150101_100301@': (4, 'fancy 1', tr('linux-meta-lts-grumpy/1')),
        }})

        self.run_it(
            [('linux-image-generic', {'Source': 'linux-meta'}, None),
             ('linux-image-grumpy-generic', {'Source': 'linux-meta-lts-grumpy'}, None),
             ('linux-image-64only', {'Source': 'linux-meta-64only', 'Architecture': 'amd64'}, None),
             ],
            {'linux-meta': (True, {'fancy/1': {'amd64': 'PASS', 'i386': 'PASS'}}),
             # we don't have an explicit result for amd64
             'linux-meta-lts-grumpy': (False, {'fancy/1': {'amd64': 'RUNNING', 'i386': 'ALWAYSFAIL'}}),
             'linux-meta-64only': (True, {'fancy/1': {'amd64': 'PASS'}}),
             })

        self.assertEqual(self.pending_requests,
                         {'linux-meta-lts-grumpy/1': {'fancy': ['amd64']}})

    def test_kernel_triggered_tests(self):
        '''linux, lxc, glibc, systemd, snapd tests get triggered by linux-meta* uploads'''

        self.data.add('libc6-dev', False, {'Source': 'glibc', 'Depends': 'linux-libc-dev'},
                      testsuite='autopkgtest')
        self.data.add('libc6-dev', True, {'Source': 'glibc', 'Depends': 'linux-libc-dev'},
                      testsuite='autopkgtest')
        self.data.add('lxc', False, {}, testsuite='autopkgtest')
        self.data.add('lxc', True, {}, testsuite='autopkgtest')
        self.data.add('systemd', False, {}, testsuite='autopkgtest')
        self.data.add('systemd', True, {}, testsuite='autopkgtest')
        self.data.add('snapd', False, {}, testsuite='autopkgtest')
        self.data.add('snapd', True, {}, testsuite='autopkgtest')
        self.data.add('linux-image-1', False, {'Source': 'linux'}, testsuite='autopkgtest')
        self.data.add('linux-libc-dev', False, {'Source': 'linux'}, testsuite='autopkgtest')
        self.data.add('linux-image', False, {'Source': 'linux-meta', 'Depends': 'linux-image-1'})

        self.set_results({'autopkgtest-testing': {
            'testing/amd64/l/lxc/20150101_100101@': (0, 'lxc 0.1', tr('passedbefore/1'))
        }})

        exc = self.run_it(
            [('linux-image', {'Version': '2', 'Depends': 'linux-image-2', 'Source': 'linux-meta'}, None),
             ('linux-image-64only', {'Source': 'linux-meta-64only', 'Architecture': 'amd64'}, None),
             ('linux-image-2', {'Version': '2', 'Source': 'linux'}, 'autopkgtest'),
             ('linux-libc-dev', {'Version': '2', 'Source': 'linux'}, 'autopkgtest'),
            ],
            {'linux-meta': (False, {'lxc': {'amd64': 'RUNNING', 'i386': 'RUNNING-ALWAYSFAIL'},
                                    'glibc': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                    'linux': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                    'systemd': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                    'snapd': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                                   }),
             'linux-meta-64only': (False, {'lxc': {'amd64': 'RUNNING'}}),
             'linux': (False, {}),
            })[1]
        # the kernel itself should not trigger tests; we want to trigger
        # everything from -meta
        self.assertEqual(exc['linux']['policy_info']['autopkgtest'], {'verdict': 'PASS'})

    def test_kernel_waits_on_meta(self):
        '''linux waits on linux-meta'''

        self.data.add('dkms', False, {})
        self.data.add('dkms', True, {})
        self.data.add('fancy-dkms', False, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'}, testsuite='autopkgtest-pkg-dkms')
        self.data.add('fancy-dkms', True, {'Source': 'fancy', 'Depends': 'dkms (>= 1)'}, testsuite='autopkgtest-pkg-dkms')
        self.data.add('linux-image-generic', False, {'Version': '0.1', 'Source': 'linux-meta', 'Depends': 'linux-image-1'})
        self.data.add('linux-image-1', False, {'Source': 'linux'}, testsuite='autopkgtest')
        self.data.add('linux-firmware', False, {'Source': 'linux-firmware'}, testsuite='autopkgtest')

        self.set_results({'autopkgtest-testing': {
            'testing/i386/f/fancy/20150101_090000@': (0, 'fancy 0.5', tr('passedbefore/1')),
            'testing/i386/l/linux/20150101_100000@': (0, 'linux 2', tr('linux-meta/0.2')),
            'testing/amd64/l/linux/20150101_100000@': (0, 'linux 2', tr('linux-meta/0.2')),
            'testing/i386/l/linux-firmware/20150101_100000@': (0, 'linux-firmware 2', tr('linux-firmware/2')),
            'testing/amd64/l/linux-firmware/20150101_100000@': (0, 'linux-firmware 2', tr('linux-firmware/2')),
        }})

        self.run_it(
            [('linux-image-generic', {'Version': '0.2', 'Source': 'linux-meta', 'Depends': 'linux-image-2'}, None),
             ('linux-image-2', {'Version': '2', 'Source': 'linux'}, 'autopkgtest'),
             ('linux-firmware', {'Version': '2', 'Source': 'linux-firmware'}, 'autopkgtest'),
            ],
            {'linux-meta': (False, {'fancy': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                                    'linux/2': {'amd64': 'PASS', 'i386': 'PASS'}
                                   }),
             # no tests, but should wait on linux-meta
             'linux': (False, {}),
             # this one does not have a -meta, so don't wait
             'linux-firmware': (True, {'linux-firmware/2': {'amd64': 'PASS', 'i386': 'PASS'}}),
            },
            {'linux': [('excuses', '<a href="#linux-meta">linux-meta</a> is not a candidate'),
                       ('dependencies', {'migrate-after': ['linux-meta']})]
            }
        )

        # now linux-meta is ready to go
        self.set_results({'autopkgtest-testing': {
            'testing/i386/f/fancy/20150101_100000@': (0, 'fancy 1', tr('linux-meta/0.2')),
            'testing/amd64/f/fancy/20150101_100000@': (0, 'fancy 1', tr('linux-meta/0.2')),
        }})
        self.run_it(
            [],
            {'linux-meta': (True, {'fancy/1': {'amd64': 'PASS', 'i386': 'PASS'},
                                   'linux/2': {'amd64': 'PASS', 'i386': 'PASS'}}),
             'linux': (True, {}),
             'linux-firmware': (True, {'linux-firmware/2': {'amd64': 'PASS', 'i386': 'PASS'}}),
            },
            {'linux': [('dependencies', {'migrate-after': ['linux-meta']})]
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
        self.set_results({'autopkgtest-testing': {
            'testing/i386/b/binutils/20150101_100000@': (0, 'binutils 1', tr('passedbefore/1')),
        }})

        exc = self.run_it(
            [('libgcc1', {'Source': 'gcc-5', 'Version': '2'}, None)],
            {'gcc-5': (False, {'binutils': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'linux': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'}})})[1]
        self.assertNotIn('notme 1', exc['gcc-5']['policy_info']['autopkgtest'])

    def test_gcc_hastest(self):
        """gcc triggers itself when it has a testsuite"""

        self.data.add('gcc-7', False, {}, testsuite='autopkgtest')

        # gcc-7 has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.set_results({'autopkgtest-series': {
            'series/i386/g/gcc-7/20150101_100000@': (0, 'gcc-7 1', tr('passedbefore/1')),
        }})

        exc = self.run_it(
            [('gcc-7', {'Source': 'gcc-7', 'Version': '2'}, 'autopkgtest')],
            {'gcc-7': (True, {'gcc-7': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'}})})[1]
        self.assertIn('gcc-7', exc['gcc-7']['policy_info']['autopkgtest'])

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

        self.data.add_default_packages(green=False)

        exc = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (True, {})},
            {'green': [('old-version', '1'), ('new-version', '2')]})[1]
        self.assertNotIn('autopkgtest', exc['green']['policy_info'])

        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, None)

    def test_ppas(self):
        '''Run test requests with additional PPAs'''

        self.data.add_default_packages(lightgreen=False)

        for line in fileinput.input(self.britney_conf, inplace=True):
            if line.startswith('ADT_PPAS'):
                print('ADT_PPAS = joe/foo awesome-developers/staging')
            else:
                sys.stdout.write(line)

        exc = self.run_it(
            [('lightgreen', {'Version': '2'}, 'autopkgtest')],
            {'lightgreen': (True, {'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL'}})},
            {'lightgreen': [('old-version', '1'), ('new-version', '2')]}
        )[1]
        self.assertEqual(exc['lightgreen']['policy_info']['autopkgtest'],
                         {'lightgreen': {
                             'amd64': ['RUNNING-ALWAYSFAIL',
                                       'https://autopkgtest.ubuntu.com/running',
                                       None,
                                       None,
                                       None],
                             'i386': ['RUNNING-ALWAYSFAIL',
                                      'https://autopkgtest.ubuntu.com/running',
                                      None,
                                      None,
                                      None]},
                          'verdict': 'PASS'})

        for arch in ['i386', 'amd64']:
            self.assertTrue(
                ('debci-ppa-testing-%s:lightgreen {"triggers": ["lightgreen/2"], '
                 '"ppas": ["joe/foo", "awesome-developers/staging"]}') % arch in self.amqp_requests or
                ('debci-ppa-testing-%s:lightgreen {"ppas": ["joe/foo", '
                 '"awesome-developers/staging"], "triggers": ["lightgreen/2"]}') % arch in self.amqp_requests,
                self.amqp_requests)
        self.assertEqual(len(self.amqp_requests), 2)

        # add results to PPA specific swift container
        self.set_results({'autopkgtest-testing-awesome-developers-staging': {
            'testing/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1', tr('passedbefore/1')),
            'testing/i386/l/lightgreen/20150101_100100@': (4, 'lightgreen 2', tr('lightgreen/2')),
            'testing/amd64/l/lightgreen/20150101_100101@': (0, 'lightgreen 2', tr('lightgreen/2')),
        }})

        exc = self.run_it(
            [],
            {'lightgreen': (False, {'lightgreen/2': {'i386': 'REGRESSION', 'amd64': 'PASS'}})},
            {'lightgreen': [('old-version', '1'), ('new-version', '2')]}
        )[1]
        self.assertEqual(
            exc['lightgreen']['policy_info']['autopkgtest'],
            {'lightgreen/2': {
                'amd64': [
                    'PASS',
                    'http://localhost:18085/autopkgtest-testing-awesome-developers-staging/'
                    'testing/amd64/l/lightgreen/20150101_100101@/log.gz',
                    None,
                    'http://localhost:18085/autopkgtest-testing-awesome-developers-staging/'
                    'testing/amd64/l/lightgreen/20150101_100101@/artifacts.tar.gz',
                    None],
                'i386': [
                    'REGRESSION',
                    'http://localhost:18085/autopkgtest-testing-awesome-developers-staging/'
                    'testing/i386/l/lightgreen/20150101_100100@/log.gz',
                    None,
                    'http://localhost:18085/autopkgtest-testing-awesome-developers-staging/'
                    'testing/i386/l/lightgreen/20150101_100100@/artifacts.tar.gz',
                    'https://autopkgtest.ubuntu.com/request.cgi?release=testing&arch=i386&package=lightgreen&'
                    'trigger=lightgreen%2F2&ppa=joe%2Ffoo&ppa=awesome-developers%2Fstaging']},
             'verdict': 'REJECTED_PERMANENTLY'})
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

    def test_disable_upgrade_tester(self):
        '''Run without second stage upgrade tester'''

        self.data.add_default_packages(green=False)

        self.data.add('green', True, {'Depends': 'libc6 (>= 0.9), libgreen1',
                                      'Conflicts': 'blue', 'Version': '2'},
                      testsuite='autopkgtest')

        self.data.compute_migrations = '--no-compute-migrations'

        self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {})[1]

        self.assertFalse(os.path.exists(os.path.join(self.data.path, 'output', 'output.txt')))
        self.assertNotEqual(self.amqp_requests, set())
        # must still record pending tests
# ## Not sure why this doesn't work in the debian env.
# #        self.assertEqual(self.pending_requests, {'green/2': {'green': ['amd64', 'i386'],
# #                                                             'darkgreen': ['amd64', 'i386'],
# #                                                             'lightgreen': ['amd64', 'i386']}})

    def test_shared_results_cache(self):
        '''Run with shared r/o autopkgtest-results.cache'''

        self.data.add_default_packages(lightgreen=False)

        # first run to create autopkgtest-results.cache
        self.set_results({'autopkgtest-testing': {
            'testing/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 2', tr('lightgreen/2')),
            'testing/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 2', tr('lightgreen/2')),
        }})

        self.run_it(
            [('lightgreen', {'Version': '2', 'Depends': 'libc6'}, 'autopkgtest')],
            {'lightgreen': (True, {'lightgreen/2': {'i386': 'PASS', 'amd64': 'PASS'}})},
        )

        # move and remember original contents
        local_path = os.path.join(self.data.path, 'data/testing/state/autopkgtest-results.cache')
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
        self.set_results({'autopkgtest-testing': {
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 3', tr('lightgreen/3')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 3', tr('lightgreen/3')),
        }})

        self.data.remove_all(True)
        self.run_it(
            [('lightgreen', {'Version': '3', 'Depends': 'libc6'}, 'autopkgtest')],
            {'lightgreen': (True, {'lightgreen/3': {'i386': 'PASS', 'amd64': 'PASS'}})},
        )

        # leaves autopkgtest-results.cache untouched
        self.assertFalse(os.path.exists(local_path))
        with open(shared_path) as f:
            self.assertEqual(orig_contents, f.read())


    def test_swift_url_is_file(self):
        '''Run without swift but with debci file (as Debian does)'''
        '''Based on test_multi_rdepends_with_tests_regression'''
        '''Multiple reverse dependencies with tests (regression)'''

        debci_file = os.path.join(self.data.path, 'debci.output')

        # Don't use swift but debci output file
        for line in fileinput.input(self.britney_conf, inplace=True):
            if line.startswith('ADT_SWIFT_URL'):
                print('ADT_SWIFT_URL     = file://%s' % debci_file)
            else:
                sys.stdout.write(line)

        with open(debci_file, 'w') as f:
            f.write('''
{
  "until": 12345,
  "results": [
  {"trigger": "green/2", "package": "darkgreen",  "arch": "i386",  "version": "1", "status": "pass",
   "run_id": "100000", "suite": "testing", "updated_at": "2018-10-04T11:18:00.000Z"},
  {"trigger": "green/2", "package": "darkgreen",  "arch": "amd64", "version": "1", "status": "pass",
   "run_id": "100000", "suite": "testing", "updated_at": "2018-10-04T11:18:01.000Z"},
  {"trigger": "green/1", "package": "lightgreen", "arch": "i386",  "version": "1", "status": "pass",
   "run_id": "101000", "suite": "testing", "updated_at": "2018-10-04T11:18:02.000Z"},
  {"trigger": "green/2", "package": "lightgreen", "arch": "i386",  "version": "1", "status": "fail",
   "run_id": "101001", "suite": "testing", "updated_at": "2018-10-04T11:18:03.000Z"},
  {"trigger": "green/1", "package": "lightgreen", "arch": "amd64", "version": "1", "status": "pass",
   "run_id": "101000", "suite": "testing", "updated_at": "2018-10-04T11:18:04.000Z"},
  {"trigger": "green/2", "package": "lightgreen", "arch": "amd64", "version": "1", "status": "fail",
   "run_id": "101001", "suite": "testing", "updated_at": "2018-10-04T11:18:05.000Z"},
  {"trigger": "green/2", "package": "green",      "arch": "i386",  "version": "2", "status": "pass",
   "run_id": "102000", "suite": "testing", "updated_at": "2018-10-04T11:18:06.000Z"},
  {"trigger": "green/1", "package": "green",      "arch": "amd64", "version": "2", "status": "pass",
   "run_id": "102000", "suite": "testing", "updated_at": "2018-10-04T11:18:07.000Z"},
  {"trigger": "green/2", "package": "green",      "arch": "amd64", "version": "2", "status": "fail",
   "run_id": "102001", "suite": "testing", "updated_at": "2018-10-04T11:18:08.000Z"}
  ]
}
''')

        self.data.add_default_packages(green=False)

        out, exc, _ = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/2': {'amd64': 'REGRESSION', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'REGRESSION', 'i386': 'REGRESSION'},
                               'darkgreen/1': {'amd64': 'PASS', 'i386': 'PASS'},
                               })
             },
            {'green': [('old-version', '1'), ('new-version', '2')]}
        )

        # should have links to log and history, but no artifacts (as this is
        # not a PPA)
        self.assertEqual(exc['green']['policy_info']['autopkgtest']['lightgreen/1']['amd64'][0],
                         'REGRESSION')
        link = urllib.parse.urlparse(exc['green']['policy_info']['autopkgtest']['lightgreen/1']['amd64'][1])
        self.assertEqual(link.path[-53:], '/autopkgtest/testing/amd64/l/lightgreen/101001/log.gz')
        self.assertEqual(exc['green']['policy_info']['autopkgtest']['lightgreen/1']['amd64'][2:4],
                         ['https://autopkgtest.ubuntu.com/packages/l/lightgreen/testing/amd64',
                          None])

        # should have retry link for the regressions (not a stable URL, test
        # separately)
        link = urllib.parse.urlparse(exc['green']['policy_info']['autopkgtest']['lightgreen/1']['amd64'][4])
        self.assertEqual(link.netloc, 'autopkgtest.ubuntu.com')
        self.assertEqual(link.path, '/request.cgi')
        self.assertEqual(urllib.parse.parse_qs(link.query),
                         {'release': ['testing'], 'arch': ['amd64'],
                          'package': ['lightgreen'], 'trigger': ['green/2']})

        # we already had all results before the run, so this should not trigger
        # any new requests
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, {})

        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_with_tests_mixed_penalty(self):
        '''Bounty/penalty system instead of blocking
        based on "Multiple reverse dependencies with tests (mixed results)"'''

        # Don't use policy verdics, but age packages appropriate
        for line in fileinput.input(self.britney_conf, inplace=True):
            if line.startswith('MINDAYS_MEDIUM'):
                print('MINDAYS_MEDIUM = 13')
            elif line.startswith('ADT_SUCCESS_BOUNTY'):
                print('ADT_SUCCESS_BOUNTY     = 6')
            elif line.startswith('ADT_REGRESSION_PENALTY'):
                print('ADT_REGRESSION_PENALTY = 27')
            else:
                sys.stdout.write(line)

        self.data.add_default_packages(green=False)

        # green has passed before on i386 only, therefore ALWAYSFAIL on amd64
        self.set_results({'autopkgtest-testing': {
            'testing/i386/g/green/20150101_100000@': (0, 'green 1', tr('passedbefore/1')),
        }})

        # first run requests tests and marks them as pending
        exc = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING'},
                               'lightgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'RUNNING-ALWAYSFAIL'},
                               })
             },
            {'green': [('old-version', '1'), ('new-version', '2')]})[1]

        # while no autopkgtest results are known, penalty applies
        self.assertEqual(exc['green']['policy_info']['age']['age-requirement'], 40)

        # second run collects the results
        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100201@': (4, 'green 2', tr('green/2')),
            # unrelated results (wrong trigger), ignore this!
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/1')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('blue/1')),
        }})

        res = self.run_it(
            [],
            {'green': (False, {'green/2': {'amd64': 'ALWAYSFAIL', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'REGRESSION', 'i386': 'RUNNING'},
                               'darkgreen/1': {'amd64': 'RUNNING', 'i386': 'PASS'},
                               })
             })
        out = res[0]
        exc = res[1]

        self.assertIn('Update Excuses generation completed', out)
        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out)

        # there should be some pending ones
        self.assertEqual(self.pending_requests,
                         {'green/2': {'darkgreen': ['amd64'], 'lightgreen': ['i386']}})

        # autopkgtest should not cause the package to be blocked
        self.assertEqual(exc['green']['policy_info']['autopkgtest']['verdict'], 'PASS')
        # instead, it should cause the age to sky-rocket
        self.assertEqual(exc['green']['policy_info']['age']['age-requirement'], 40)

    def test_multi_rdepends_with_tests_no_penalty(self):
        '''Check that penalties are not applied for "urgency >= high"'''

        # Don't use policy verdics, but age packages appropriate
        for line in fileinput.input(self.britney_conf, inplace=True):
            if line.startswith('MINDAYS_MEDIUM'):
                print('MINDAYS_MEDIUM = 13')
            elif line.startswith('ADT_SUCCESS_BOUNTY'):
                print('ADT_SUCCESS_BOUNTY     = 6')
            elif line.startswith('ADT_REGRESSION_PENALTY'):
                print('ADT_REGRESSION_PENALTY = 27')
            elif line.startswith('NO_PENALTIES'):
                print('NO_PENALTIES = medium')
            else:
                sys.stdout.write(line)

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/1')),
            'testing/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100201@': (4, 'green 2', tr('green/2')),
        }})

        exc = self.run_it(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            {'green': (False, {'green/2': {'amd64': 'ALWAYSFAIL', 'i386': 'PASS'},
                               'lightgreen/1': {'amd64': 'REGRESSION', 'i386': 'RUNNING-ALWAYSFAIL'},
                               'darkgreen/1': {'amd64': 'RUNNING-ALWAYSFAIL', 'i386': 'PASS'},
                               })
             })[1]

        # age-requirement should remain the same despite regression
        self.assertEqual(exc['green']['policy_info']['age']['age-requirement'], 13)

    def test_passing_package_receives_bounty(self):
        '''Test bounty system (instead of policy verdict)'''

        # Don't use policy verdics, but age packages appropriate
        for line in fileinput.input(self.britney_conf, inplace=True):
            if line.startswith('MINDAYS_MEDIUM'):
                print('MINDAYS_MEDIUM = 13')
            elif line.startswith('ADT_SUCCESS_BOUNTY'):
                print('ADT_SUCCESS_BOUNTY     = 6')
            elif line.startswith('ADT_REGRESSION_PENALTY'):
                print('ADT_REGRESSION_PENALTY = 27')
            else:
                sys.stdout.write(line)

        self.data.add_default_packages(green=False)

        self.set_results({'autopkgtest-testing': {
            'testing/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1', tr('green/2')),
            'testing/i386/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'testing/amd64/l/lightgreen/20150101_100100@': (0, 'lightgreen 1', tr('green/2')),
            'testing/i386/g/green/20150101_100200@': (0, 'green 2', tr('green/2')),
            'testing/amd64/g/green/20150101_100201@': (0, 'green 2', tr('green/2')),
        }})

        exc = self.run_it(
            [('green', {'Version': '2'}, 'autopkgtest')],
            {'green': (False, {})},
            {})[1]

        # it should cause the age to drop
        self.assertEqual(exc['green']['policy_info']['age']['age-requirement'], 8)
        self.assertEqual(exc['green']['excuses'][-1], 'Required age is not allowed to drop below 8 days')


if __name__ == '__main__':
    unittest.main()
