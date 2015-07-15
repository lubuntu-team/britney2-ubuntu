#!/usr/bin/python
# (C) 2014 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import apt_pkg
import operator
import os
import sys
import subprocess
import fileinput
import unittest
import json

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from autopkgtest import ADT_EXCUSES_LABELS
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

        # fake adt-britney script; necessary until we drop that code
        self.adt_britney = os.path.join(
            self.data.home, 'auto-package-testing', 'jenkins', 'adt-britney')
        os.makedirs(os.path.dirname(self.adt_britney))
        with open(self.adt_britney, 'w') as f:
            f.write('''#!/bin/sh -e
touch $HOME/proposed-migration/autopkgtest/work/adt.request.series
echo "$@" >> /%s/adt-britney.log ''' % self.data.path)
        os.chmod(self.adt_britney, 0o755)

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

        #print('-------\nexcuses: %s\n-----' % excuses)
        #print('-------\nout: %s\n-----' % out)
        #print('run:\n%s -c %s\n' % (self.britney, self.britney_conf))
        #subprocess.call(['bash', '-i'], cwd=self.data.path)
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

    def test_multi_rdepends_with_tests_all_running(self):
        '''Multiple reverse dependencies with tests (all running)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            # FIXME: while we only submit requests through AMQP, but don't consider
            # their results, we don't expect this to hold back stuff.
            VALID_CANDIDATE,
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
        self.do_test([], VALID_CANDIDATE, [r'\bgreen\b.*>1</a> to .*>2<'])
        self.assertEqual(self.amqp_requests, set())
        # but the set of pending tests doesn't change
        self.assertEqual(self.pending_requests, expected_pending)

    def test_multi_rdepends_with_tests_all_pass(self):
        '''Multiple reverse dependencies with tests (all pass)'''

        # first run requests tests and marks them as pending
        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest')],
            # FIXME: while we only submit requests through AMQP, but don't consider
            # their results, we don't expect this to hold back stuff.
            VALID_CANDIDATE,
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
                         ['20150101_100200@', {'1': [False, []],
                                               '2': [True, [['green', '2']]]}])
        self.assertEqual(res['lightgreen']['amd64'],
                         ['20150101_100101@', {'1': [True, [['green', '2']]]}])

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
            # FIXME: while we only submit requests through AMQP, but don't consider
            # their results, we don't expect this to hold back stuff.
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*in progress.*i386.*in progress',
             r'autopkgtest for lightgreen 1: .*amd64.*in progress.*i386.*in progress',
             r'autopkgtest for darkgreen 1: .*amd64.*in progress.*i386.*in progress'])

        # second run collects the results
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/d/darkgreen/20150101_100000@': (0, 'darkgreen 1'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 1'),
            'series/i386/g/green/20150101_100200@': (0, 'green 2'),
            'series/amd64/g/green/20150101_100201@': (4, 'green 2'),
        }})

        out = self.do_test(
            [],
            # FIXME: while we only submit requests through AMQP, but don't consider
            # their results, we don't expect this to hold back stuff.
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for green 2: .*amd64.*Regression.*i386.*Pass',
             r'autopkgtest for lightgreen 1: .*amd64.*Regression.*i386.*in progress',
             r'autopkgtest for darkgreen 1: .*amd64.*in progress.*i386.*Pass'])

        # not expecting any failures to retrieve from swift
        self.assertNotIn('Failure', out, out)

        # there should be some pending ones
        self.assertIn('darkgreen 1 amd64 green 2', self.pending_requests)
        self.assertIn('lightgreen 1 i386 green 2', self.pending_requests)

    def test_package_pair_running(self):
        '''Two packages in unstable that need to go in together (running)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'}, 'autopkgtest'),
             ('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 2)'}, 'autopkgtest')],
            # FIXME: while we only submit requests through AMQP, but don't consider
            # their results, we don't expect this to hold back stuff.
            VALID_CANDIDATE,
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

    def test_tmpfail(self):
        '''tmpfail result is considered a failure'''

        # one tmpfail result without testpkg-version
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100101@': (16, None),
            'series/amd64/l/lightgreen/20150101_100101@': (16, 'lightgreen 2'),
        }})

        self.do_test(
            [('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 1)'}, 'autopkgtest')],
            # FIXME: while we only submit requests through AMQP, but don't consider
            # their results, we don't expect this to hold back stuff.
            VALID_CANDIDATE,
            [r'\blightgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for lightgreen 2: .*amd64.*Regression.*i386.*Regression'],
            ['in progress'])

        self.assertEqual(self.pending_requests, '')

    def test_rerun_failure(self):
        '''manually re-running a failed test gets picked up'''

        # first run fails
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 2'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 2'),
        }})

        self.do_test(
            [('lightgreen', {'Version': '2', 'Depends': 'libgreen1 (>= 1)'}, 'autopkgtest')],
            # FIXME: while we only submit requests through AMQP, but don't consider
            # their results, we don't expect this to hold back stuff.
            VALID_CANDIDATE,
            [r'\blightgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for lightgreen 2: .*amd64.*Regression.*i386.*Regression'])
        self.assertEqual(self.pending_requests, '')

        # re-running test manually succeeded
        self.swift.set_results({'autopkgtest-series': {
            'series/i386/l/lightgreen/20150101_100101@': (4, 'lightgreen 2'),
            'series/amd64/l/lightgreen/20150101_100101@': (4, 'lightgreen 2'),
            'series/i386/l/lightgreen/20150101_100201@': (0, 'lightgreen 2'),
            'series/amd64/l/lightgreen/20150101_100201@': (0, 'lightgreen 2'),
        }})
        self.do_test(
            [], VALID_CANDIDATE,
            [r'\blightgreen\b.*>1</a> to .*>2<',
             r'autopkgtest for lightgreen 2: .*amd64.*Pass.*i386.*Pass'])
        self.assertEqual(self.pending_requests, '')

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


class TestAdtBritney(TestBase):
    '''Legacy adt-britney/lp:auto-package-testing interface'''

    def setUp(self):
        super(TestAdtBritney, self).setUp()

        # Mofify configuration according to the test context.
        with open(self.britney_conf, 'r') as fp:
            original_config = fp.read()
        # Disable boottests.
        new_config = original_config.replace(
            'BOOTTEST_ENABLE   = yes', 'BOOTTEST_ENABLE   = no')
        with open(self.britney_conf, 'w') as fp:
            fp.write(new_config)

        # fake adt-britney script
        self.adt_britney = os.path.join(
            self.data.home, 'auto-package-testing', 'jenkins', 'adt-britney')
        os.makedirs(os.path.dirname(self.adt_britney))

        with open(self.adt_britney, 'w') as f:
            f.write('''#!/bin/sh -e
echo "$@" >> /%s/adt-britney.log ''' % self.data.path)
        os.chmod(self.adt_britney, 0o755)

        # add a bunch of packages to testing to avoid repetition
        self.data.add('libc6', False)
        self.data.add('libgreen1', False, {'Source': 'green',
                                           'Depends': 'libc6 (>= 0.9)'})
        self.data.add('green', False, {'Depends': 'libc6 (>= 0.9), libgreen1',
                                       'Conflicts': 'blue'})
        self.data.add('lightgreen', False, {'Depends': 'libgreen1'})
        self.data.add('darkgreen', False, {'Depends': 'libgreen1'})
        self.data.add('blue', False, {'Depends': 'libc6 (>= 0.9)',
                                      'Conflicts': 'green'})
        self.data.add('justdata', False, {'Architecture': 'all'})

    def __merge_records(self, results, history=""):
        '''Merges a list of results with records in history.

        This function merges results from a collect with records already in
        history and sort records by version/name of causes and version/name of
        source packages with tests. This should be done in the fake
        adt-britney but it is more convenient to just pass a static list of
        records and make adt-britney just return this list.
        '''

        if history is None:
            history = ""
        records = [x.split() for x in (results.strip() + '\n' +
                                       history.strip()).split('\n') if x]

        records.sort(cmp=apt_pkg.version_compare, key=operator.itemgetter(4))
        records.sort(key=operator.itemgetter(3))
        records.sort(cmp=apt_pkg.version_compare, key=operator.itemgetter(1))
        records.sort()

        return "\n".join([' '.join(x) for x in records])

    def make_adt_britney(self, request, history=""):
        with open(self.adt_britney, 'w') as f:
            f.write('''#!%(py)s
import argparse, shutil,sys

def request():
    if args.req:
        shutil.copy(args.req, '%(path)s/adt-britney.requestarg')
    with open(args.output, 'w') as f:
        f.write("""%(rq)s""".replace('PASS', 'NEW').replace('FAIL', 'NEW').replace('RUNNING', 'NEW'))

def submit():
    with open(args.req, 'w') as f:
        f.write("""%(rq)s""".replace('PASS', 'RUNNING').
                    replace('FAIL', 'RUNNING'))

def collect():
    with open(args.output, 'w') as f:
        f.write("""%(res)s""")

p = argparse.ArgumentParser()
p.add_argument('-c', '--config')
p.add_argument('-a', '--arch')
p.add_argument('-r', '--release')
p.add_argument('-P', '--use-proposed', action='store_true')
p.add_argument('-d', '--debug', action='store_true')
p.add_argument('-U', '--no-update', action='store_true')
sp = p.add_subparsers()

prequest = sp.add_parser('request')
prequest.add_argument('-O', '--output')
prequest.add_argument('req', nargs='?')
prequest.set_defaults(func=request)

psubmit = sp.add_parser('submit')
psubmit.add_argument('req')
psubmit.set_defaults(func=submit)

pcollect = sp.add_parser('collect')
pcollect.add_argument('-O', '--output')
pcollect.add_argument('-n', '--new-only', action='store_true', default=False)
pcollect.set_defaults(func=collect)

args = p.parse_args()
args.func()
                    ''' % {'py': sys.executable, 'path': self.data.path,
                           'rq': request,
                           'res': self.__merge_records(request, history)})

    def do_test(self, unstable_add, adt_request, considered, expect=None,
                no_expect=None, history=""):
        for (pkg, fields) in unstable_add:
            self.data.add(pkg, True, fields)

        self.make_adt_britney(adt_request, history)

        (excuses, out) = self.run_britney()
        #print('-------\nexcuses: %s\n-----' % excuses)
        #print('-------\nout: %s\n-----' % out)
        #print('run:\n%s -c %s\n' % (self.britney, self.britney_conf))
        #subprocess.call(['bash', '-i'], cwd=self.data.path)
        if considered:
            self.assertIn('Valid candidate', excuses)
        else:
            self.assertIn('Not considered', excuses)

        if expect:
            for re in expect:
                self.assertRegexpMatches(excuses, re)
        if no_expect:
            for re in no_expect:
                self.assertNotRegexpMatches(excuses, re)

    def test_no_request_for_uninstallable(self):
        '''Does not request a test for an uninstallable package'''

        self.do_test(
            # uninstallable unstable version
            [('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1 (>= 2)'})],
            'green 1.1~beta RUNNING green 1.1~beta\n',
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             'green/amd64 unsatisfiable Depends: libgreen1 \(>= 2\)'],
            # autopkgtest should not be triggered for uninstallable pkg
            ['autopkgtest'])

    def test_request_for_installable_running(self):
        '''Requests a test for an installable package, test still running'''

        self.do_test(
            [('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1'})],
            'green 1.1~beta RUNNING green 1.1~beta\n',
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             '<li>autopkgtest for green 1.1~beta: %s' % ADT_EXCUSES_LABELS['RUNNING']])

    def test_request_for_installable_first_fail(self):
        '''Requests a test for an installable package. No history and first result is a failure'''

        self.do_test(
            [('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1'})],
            'green 1.1~beta FAIL green 1.1~beta\n',
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             '<li>autopkgtest for green 1.1~beta: %s' % ADT_EXCUSES_LABELS['ALWAYSFAIL']])

    def test_request_for_installable_fail_regression(self):
        '''Requests a test for an installable package, test fail'''

        self.do_test(
            [('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1'})],
            'green 1.1~beta FAIL green 1.1~beta\n',
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             '<li>autopkgtest for green 1.1~beta: %s' % ADT_EXCUSES_LABELS['REGRESSION']],
            history='green 1.0~beta PASS green 1.0~beta\n')

    def test_request_for_installable_pass(self):
        '''Requests a test for an installable package, test pass'''

        self.do_test(
            [('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1'})],
            'green 1.1~beta PASS green 1.1~beta\n',
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             '<li>autopkgtest for green 1.1~beta: %s' % ADT_EXCUSES_LABELS['PASS']])

    def test_multi_rdepends_with_tests_running(self):
        '''Multiple reverse dependencies with tests (still running)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'})],
            'lightgreen 1 PASS green 2\n'
            'darkgreen 1 RUNNING green 2\n',
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['RUNNING']])

    def test_multi_rdepends_with_tests_fail_always(self):
        '''Multiple reverse dependencies with tests (fail)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'})],
            'lightgreen 1 PASS green 2\n'
            'darkgreen 1 FAIL green 2\n',
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['ALWAYSFAIL']])

    def test_multi_rdepends_with_tests_fail_regression(self):
        '''Multiple reverse dependencies with tests (fail)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'})],
            'lightgreen 1 PASS green 2\n'
            'darkgreen 1 FAIL green 2\n',
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['REGRESSION']],
            history='darkgreen 1 PASS green 1\n')

    def test_multi_rdepends_with_tests_pass(self):
        '''Multiple reverse dependencies with tests (pass)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'})],
            'lightgreen 1 PASS green 2\n'
            'darkgreen 1 PASS green 2\n',
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['PASS']])

    def test_multi_rdepends_with_some_tests_running(self):
        '''Multiple reverse dependencies with some tests (running)'''

        # add a third reverse dependency to libgreen1 which does not have a test
        self.data.add('mint', False, {'Depends': 'libgreen1'})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'})],
            'lightgreen 1 RUNNING green 2\n'
            'darkgreen 1 RUNNING green 2\n',
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['RUNNING'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['RUNNING']])

    def test_multi_rdepends_with_some_tests_fail_always(self):
        '''Multiple reverse dependencies with some tests (fail)'''

        # add a third reverse dependency to libgreen1 which does not have a test
        self.data.add('mint', False, {'Depends': 'libgreen1'})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'})],
            'lightgreen 1 PASS green 2\n'
            'darkgreen 1 FAIL green 2\n',
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['ALWAYSFAIL']])

    def test_multi_rdepends_with_some_tests_fail_regression(self):
        '''Multiple reverse dependencies with some tests (fail)'''

        # add a third reverse dependency to libgreen1 which does not have a test
        self.data.add('mint', False, {'Depends': 'libgreen1'})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'})],
            'lightgreen 1 PASS green 2\n'
            'darkgreen 1 FAIL green 2\n',
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['REGRESSION']],
            history='darkgreen 1 PASS green 1\n')

    def test_multi_rdepends_with_some_tests_pass(self):
        '''Multiple reverse dependencies with some tests (pass)'''

        # add a third reverse dependency to libgreen1 which does not have a test
        self.data.add('mint', False, {'Depends': 'libgreen1'})

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'green', 'Depends': 'libc6'})],
            'lightgreen 1 PASS green 2\n'
            'darkgreen 1 PASS green 2\n',
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['PASS']])

    def test_binary_from_new_source_package_running(self):
        '''building an existing binary for a new source package (running)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6'})],
            'lightgreen 1 PASS newgreen 2\n'
            'darkgreen 1 RUNNING newgreen 2\n',
            NOT_CONSIDERED,
            [r'\bnewgreen\b.*\(- to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['RUNNING']])

    def test_binary_from_new_source_package_fail_always(self):
        '''building an existing binary for a new source package (fail)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6'})],
            'lightgreen 1 PASS newgreen 2\n'
            'darkgreen 1 FAIL newgreen 2\n',
            VALID_CANDIDATE,
            [r'\bnewgreen\b.*\(- to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['ALWAYSFAIL']])

    def test_binary_from_new_source_package_fail_regression(self):
        '''building an existing binary for a new source package (fail)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6'})],
            'lightgreen 1 PASS newgreen 2\n'
            'darkgreen 1 FAIL newgreen 2\n',
            NOT_CONSIDERED,
            [r'\bnewgreen\b.*\(- to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['REGRESSION']],
            history='darkgreen 1 PASS green 1\n')

    def test_binary_from_new_source_package_pass(self):
        '''building an existing binary for a new source package (pass)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6'})],
            'lightgreen 1 PASS newgreen 2\n'
            'darkgreen 1 PASS newgreen 2\n',
            VALID_CANDIDATE,
            [r'\bnewgreen\b.*\(- to .*>2<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS'],
             '<li>autopkgtest for darkgreen 1: %s' % ADT_EXCUSES_LABELS['PASS']])

    def test_binary_from_new_source_package_uninst(self):
        '''building an existing binary for a new source package (uninstallable)'''

        self.do_test(
            [('libgreen1', {'Version': '2', 'Source': 'newgreen', 'Depends': 'libc6, nosuchpkg'})],
            'darkgreen 1 FAIL newgreen 2\n',
            NOT_CONSIDERED,
            [r'\bnewgreen\b.*\(- to .*>2<',
             'libgreen1/amd64 unsatisfiable Depends: nosuchpkg'],
            # autopkgtest should not be triggered for uninstallable pkg
            ['autopkgtest'])

    @unittest.expectedFailure
    def test_result_from_older_version(self):
        '''test result from older version than the uploaded one'''

        self.do_test(
            [('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1'})],
            'green 1.1~alpha PASS green 1.1~beta\n',
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             # it's not entirely clear what precisely it should say here
             '<li>autopkgtest for green 1.1~beta: %s' % ADT_EXCUSES_LABELS['RUNNING']])

    def test_request_for_installable_fail_regression_promoted(self):
        '''Requests a test for an installable package, test fail, is a regression.

        This test verifies a bug in britney where a package was promoted if latest test
        appeared before previous result in history, only the last result in
        alphabetic order was taken into account. For example:
            A 1 FAIL B 1
            A 1 PASS A 1
        In this case results for A 1 didn't appear in the list of results
        triggered by the upload of B 1 and B 1 was promoted
        '''

        self.do_test(
            [('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1'})],
            'lightgreen 1 FAIL green 1.1~beta\n',
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['REGRESSION']],
            history="lightgreen 1 PASS lightgreen 1"
        )

    def test_history_always_passed(self):
        '''All the results in history are PASS, and test passed

        '''

        self.do_test(
            [('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1'})],
            'lightgreen 1 PASS green 1.1~beta\n',
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['PASS']],
            history="lightgreen 1 PASS lightgreen 1"
        )

    def test_history_always_failed(self):
        '''All the results in history are FAIL, test fails. not a regression.

        '''

        self.do_test(
            [('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1'})],
            'lightgreen 1 FAIL green 1.1~beta\n',
            VALID_CANDIDATE,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['ALWAYSFAIL']],
            history="lightgreen 1 FAIL lightgreen 1"
        )

    def test_history_regression(self):
        '''All the results in history are PASS, test fails. Blocked.

        '''
        self.do_test(
            [('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9), libgreen1'})],
            'lightgreen 1 FAIL green 1.1~beta\n',
            NOT_CONSIDERED,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             '<li>autopkgtest for lightgreen 1: %s' % ADT_EXCUSES_LABELS['REGRESSION']],
            history="lightgreen 1 PASS lightgreen 1"
        )

    def shell(self):
        # uninstallable unstable version
        self.data.add('yellow', True, {'Version': '1.1~beta',
                                       'Depends': 'libc6 (>= 0.9), nosuchpkg'})

        self.make_adt_britney('yellow 1.1~beta RUNNING yellow 1.1~beta\n',
                              'purple 2 FAIL pink 3.0.~britney\n')

        print('run:\n%s -c %s\n' % (self.britney, self.britney_conf))
        subprocess.call(['bash', '-i'], cwd=self.data.path)


if __name__ == '__main__':
    unittest.main()
