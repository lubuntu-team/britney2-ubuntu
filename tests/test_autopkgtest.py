#!/usr/bin/python
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

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from tests import TestBase, mock_swift

NOT_CONSIDERED = False
VALID_CANDIDATE = True


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
        self.data.add('justdata', False, {'Architecture': 'all'})

        # create mock Swift server (but don't start it yet, as tests first need
        # to poke in results)
        self.swift = mock_swift.AutoPkgTestSwiftServer(port=18085)
        self.swift.set_results({})

    def tearDown(self):
        del self.swift

    def do_test(self, unstable_add, considered, excuses_expect=None, excuses_no_expect=None):
        for (pkg, fields, testsuite) in unstable_add:
            self.data.add(pkg, True, fields, True, testsuite)

        self.swift.start()
        (excuses, out) = self.run_britney()
        self.swift.stop()

        # print('-------\nexcuses: %s\n-----' % excuses)
        if 'SHOW_OUTPUT' in os.environ:
            print('-------\nout: %s\n-----' % out)
        if considered:
            self.assertIn('Valid candidate', excuses)
        else:
            self.assertIn('Not considered', excuses)

        if excuses_expect:
            for re in excuses_expect:
                self.assertRegexpMatches(excuses, re, excuses)
        if excuses_no_expect:
            for re in excuses_no_expect:
                self.assertNotRegexpMatches(excuses, re, excuses)

        self.amqp_requests = set()
        try:
            with open(self.fake_amqp) as f:
                for line in f:
                    self.amqp_requests.add(line.strip())
        except IOError:
            pass

        try:
            with open(os.path.join(self.data.path, 'data/series-proposed/autopkgtest/pending.txt')) as f:
                self.pending_requests = f.read()
        except IOError:
                self.pending_requests = None

        return out

    def test_no_request_for_uninstallable(self):
        '''Does not request a test for an uninstallable package'''

        self.do_test(
            # uninstallable unstable version
            [('lightgreen', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1 (>= 2)'}, 'autopkgtest')],
            NOT_CONSIDERED,
            [r'\blightgreen\b.*>1</a> to .*>1.1~beta<',
             'lightgreen/amd64 unsatisfiable Depends: libgreen1 \(>= 2\)'],
            # autopkgtest should not be triggered for uninstallable pkg
            ['autopkgtest'])

        self.assertEqual(self.pending_requests, '')
        self.assertEqual(self.amqp_requests, set())

    def test_multi_rdepends_with_tests_all_running(self):
        '''Multiple reverse dependencies with tests (all running)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*in progress.*i386.*in progress',
             r'autopkgtest for lightgreen 1: .*amd64.*in progress.*i386.*in progress',
             r'autopkgtest for darkgreen 1: .*amd64.*in progress.*i386.*in progress'])

        # we expect the package's and its reverse dependencies' tests to get
        # triggered
        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:green', 'debci-series-amd64:green',
                 'debci-series-i386:lightgreen', 'debci-series-amd64:lightgreen',
                 'debci-series-i386:darkgreen', 'debci-series-amd64:darkgreen']))
        os.unlink(self.fake_amqp)

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
        self.do_test([], NOT_CONSIDERED, [r'\bgreen\b.*>1</a> to .*>2<'])
        self.assertEqual(self.amqp_requests, set())
        # but the set of pending tests doesn't change
        self.assertEqual(self.pending_requests, expected_pending)

    def test_multi_rdepends_with_tests_all_pass(self):
        '''Multiple reverse dependencies with tests (all pass)'''

        # first run requests tests and marks them as pending
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*in progress.*i386.*in progress',
             r'autopkgtest for lightgreen 1: .*amd64.*in progress.*i386.*in progress',
             r'autopkgtest for darkgreen 1: .*amd64.*in progress.*i386.*in progress'])

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
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Pass.*i386.*Pass',
             r'autopkgtest for lightgreen 1: .*amd64.*Pass.*i386.*Pass',
             r'autopkgtest for darkgreen 1: .*amd64.*Pass.*i386.*Pass'])

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
        os.unlink(self.fake_amqp)
        self.swift.set_results({})
        out = self.do_test(
            [],
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Pass.*i386.*Pass',
             r'autopkgtest for lightgreen 1: .*amd64.*Pass.*i386.*Pass',
             r'autopkgtest for darkgreen 1: .*amd64.*Pass.*i386.*Pass'])
        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, '')
        self.assertNotIn('Failure', out, out)

    def test_multi_rdepends_with_tests_mixed(self):
        '''Multiple reverse dependencies with tests (mixed results)'''

        # first run requests tests and marks them as pending
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*in progress.*i386.*in progress',
             r'autopkgtest for lightgreen 1: .*amd64.*in progress.*i386.*in progress',
             r'autopkgtest for darkgreen 1: .*amd64.*in progress.*i386.*in progress'])

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
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Always failed.*i386.*Pass',
             r'autopkgtest for lightgreen 1: .*amd64.*Regression.*i386.*in progress',
             r'autopkgtest for darkgreen 1: .*amd64.*in progress.*i386.*Pass'])

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
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Regression.*i386.*Pass',
             r'autopkgtest for lightgreen 1: .*amd64.*Regression.*i386.*Regression',
             r'autopkgtest for darkgreen 1: .*amd64.*Pass.*i386.*Pass'])

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
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Always failed.*i386.*Pass',
             r'autopkgtest for lightgreen 1: .*amd64.*Always failed.*i386.*Always failed',
             r'autopkgtest for darkgreen 1: .*amd64.*Pass.*i386.*Pass'])

        self.assertEqual(self.pending_requests, '')
        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

    def test_package_pair_running(self):
        '''Two packages in unstable that need to go in together (running)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 2)'}, 'autopkgtest')],
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'\blightgreen\b.*>1</a> to .*>2<'])

        # we expect the package's and its reverse dependencies' tests to get
        # triggered; lightgreen should be triggered only once
        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:green', 'debci-series-amd64:green',
                 'debci-series-i386:lightgreen', 'debci-series-amd64:lightgreen',
                 'debci-series-i386:darkgreen', 'debci-series-amd64:darkgreen']))
        os.unlink(self.fake_amqp)

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
            NOT_CONSIDERED,
            [r'\bnewgreen\b.*\(- to .*>2<',
             r'autopkgtest for newgreen 2: .*amd64.*in progress.*i386.*in progress',
             r'autopkgtest for lightgreen 1: .*amd64.*in progress.*i386.*in progress',
             r'autopkgtest for darkgreen 1: .*amd64.*in progress.*i386.*in progress'])

        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:newgreen', 'debci-series-amd64:newgreen',
                 'debci-series-i386:lightgreen', 'debci-series-amd64:lightgreen',
                 'debci-series-i386:darkgreen', 'debci-series-amd64:darkgreen']))
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
            VALID_CANDIDATE,
            [r'\bnewgreen\b.*\(- to .*>2<',
             r'autopkgtest for newgreen 2: .*amd64.*Pass.*i386.*Pass',
             r'autopkgtest for lightgreen 1: .*amd64.*Pass.*i386.*Pass',
             r'autopkgtest for darkgreen 1: .*amd64.*Pass.*i386.*Pass'])

        self.assertEqual(
            self.amqp_requests,
            set(['debci-series-i386:newgreen', 'debci-series-amd64:newgreen',
                 'debci-series-i386:lightgreen', 'debci-series-amd64:lightgreen',
                 'debci-series-i386:darkgreen', 'debci-series-amd64:darkgreen']))
        self.assertEqual(self.pending_requests, '')

    def test_result_from_older_version(self):
        '''test result from older version than the uploaded one'''

        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
        }})

        self.do_test(
            [('darkgreen', {'Version': '2', 'Depends': 'libc6 (>= 0.9), libgreen1'}, 'autopkgtest')],
            NOT_CONSIDERED,
            [r'\bdarkgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for darkgreen 2: .*amd64.*in progress.*i386.*in progress'])

    def test_tmpfail(self):
        '''tmpfail result is considered a failure'''

        # one tmpfail result without testpkg-version
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/i386/l/lightgreen/20150101_100101@': (16, None),
            'series/amd64/l/lightgreen/20150101_100000@': (0, 'lightgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (16, 'lightgreen 2'),
        }})

        self.do_test(
            [('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 1)'}, 'autopkgtest')],
            NOT_CONSIDERED,
            [r'\blightgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for lightgreen 2: .*amd64.*Regression.*i386.*Regression'],
            ['in progress'])

        self.assertEqual(self.pending_requests, '')

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
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Regression.*i386.*Regression',
             r'autopkgtest for lightgreen 1: .*amd64.*Regression.*i386.*Regression'])
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
            [], VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Pass.*i386.*Pass',
             r'autopkgtest for lightgreen 1: .*amd64.*Pass.*i386.*Pass'])
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
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'\blightgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Pass.*i386.*Pass',
             r'autopkgtest for lightgreen 2: .*amd64.*Regression.*i386.*Regression'])
        self.assertEqual(self.pending_requests, '')
        os.unlink(self.fake_amqp)

        # remove new lightgreen by resetting archive indexes, and re-adding
        # green
        self.data.remove_all(True)

        # next run should re-trigger lightgreen 1 to test against green/2
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Pass.*i386.*Pass',
             r'autopkgtest for lightgreen 1: .*amd64.*Pass.*i386.*Pass'],
            ['lightgreen 2'])

        # should not trigger new requests
        self.assertEqual(self.pending_requests, '')
        self.assertEqual(self.amqp_requests,
                         set(['debci-series-amd64:lightgreen', 'debci-series-i386:lightgreen']))

        # but the next run should not trigger anything new
        os.unlink(self.fake_amqp)
        self.do_test(
            [],
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Pass.*i386.*Pass',
             r'autopkgtest for lightgreen 1: .*amd64.*Pass.*i386.*Pass'],
            ['lightgreen 2'])
        self.assertEqual(self.pending_requests, '')
        self.assertEqual(self.amqp_requests, set())

    def test_no_amqp_config(self):
        '''Run without autopkgtest requests'''

        # Disable AMQP server config
        for line in fileinput.input(self.britney_conf, inplace=True):
            if not line.startswith('ADT_AMQP') and not line.startswith('ADT_SWIFT_URL'):
                sys.stdout.write(line)

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<'], ['autopkgtest'])

        self.assertEqual(self.amqp_requests, set())
        self.assertEqual(self.pending_requests, None)


if __name__ == '__main__':
    unittest.main()
