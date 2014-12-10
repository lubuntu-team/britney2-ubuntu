#!/usr/bin/python
# (C) 2014 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import tempfile
import shutil
import os
import sys
import subprocess
import unittest
import apt_pkg
import operator

apt_pkg.init()
architectures = ['amd64', 'arm64', 'armhf', 'i386', 'powerpc', 'ppc64el']

my_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NOT_CONSIDERED = False
VALID_CANDIDATE = True

sys.path.insert(0, my_dir)
from autopkgtest import ADT_EXCUSES_LABELS


class TestData:
    def __init__(self):
        '''Construct local test package indexes.

        The archive is initially empty. You can create new packages with
        create_deb(). self.path contains the path of the archive, and
        self.apt_source provides an apt source "deb" line.

        It is kept in a temporary directory which gets removed when the Archive
        object gets deleted.
        '''
        self.path = tempfile.mkdtemp(prefix='testarchive.')
        self.apt_source = 'deb file://%s /' % self.path
        self.series = 'series'
        self.dirs = {False: os.path.join(self.path, 'data', self.series),
                     True: os.path.join(self.path, 'data', '%s-proposed' % self.series)}
        os.makedirs(self.dirs[False])
        os.mkdir(self.dirs[True])
        self.added_sources = {False: set(), True: set()}
        self.added_binaries = {False: set(), True: set()}

        # pre-create all files for all architectures
        for arch in architectures:
            for dir in self.dirs.values():
                with open(os.path.join(dir, 'Packages_' + arch), 'w'):
                    pass
        for dir in self.dirs.values():
            for fname in ['Dates', 'Blocks']:
                with open(os.path.join(dir, fname), 'w'):
                    pass
            for dname in ['Hints']:
                os.mkdir(os.path.join(dir, dname))

        os.mkdir(os.path.join(self.path, 'output'))

        # create temporary home dir for proposed-migration autopktest status
        self.home = os.path.join(self.path, 'home')
        os.environ['HOME'] = self.home
        os.makedirs(os.path.join(self.home, 'proposed-migration',
                                 'autopkgtest', 'work'))

    def __del__(self):
        shutil.rmtree(self.path)

    def add(self, name, unstable, fields={}, add_src=True):
        '''Add a binary package to the index file.

        You need to specify at least the package name and in which list to put
        it (unstable==True for unstable/proposed, or False for
        testing/release). fields specifies all additional entries, e. g.
        {'Depends': 'foo, bar', 'Conflicts: baz'}. There are defaults for most
        fields.

        Unless add_src is set to False, this will also automatically create a
        source record, based on fields['Source'] and name.
        '''
        assert (name not in self.added_binaries[unstable])
        self.added_binaries[unstable].add(name)

        fields.setdefault('Architecture', architectures[0])
        fields.setdefault('Version', '1')
        fields.setdefault('Priority', 'optional')
        fields.setdefault('Section', 'devel')
        fields.setdefault('Description', 'test pkg')
        if fields['Architecture'] == 'all':
            for a in architectures:
                self._append(name, unstable, 'Packages_' + a, fields)
        else:
            self._append(name, unstable, 'Packages_' + fields['Architecture'],
                         fields)

        if add_src:
            src = fields.get('Source', name)
            if src not in self.added_sources[unstable]:
                self.add_src(src, unstable, {'Version': fields['Version'],
                                             'Section': fields['Section']})

    def add_src(self, name, unstable, fields={}):
        '''Add a source package to the index file.

        You need to specify at least the package name and in which list to put
        it (unstable==True for unstable/proposed, or False for
        testing/release). fields specifies all additional entries, which can be
        Version (default: 1), Section (default: devel), and Extra-Source-Only.
        '''
        assert (name not in self.added_sources[unstable])
        self.added_sources[unstable].add(name)

        fields.setdefault('Version', '1')
        fields.setdefault('Section', 'devel')
        self._append(name, unstable, 'Sources', fields)

    def _append(self, name, unstable, file_name, fields):
        with open(os.path.join(self.dirs[unstable], file_name), 'a') as f:
            f.write('''Package: %s
Maintainer: Joe <joe@example.com>
''' % name)

            for k, v in fields.items():
                f.write('%s: %s\n' % (k, v))
            f.write('\n')


class Test(unittest.TestCase):
    def setUp(self):
        self.data = TestData()

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

        self.britney = os.path.join(my_dir, 'britney.py')
        self.britney_conf = os.path.join(my_dir, 'britney.conf')
        assert os.path.exists(self.britney)
        assert os.path.exists(self.britney_conf)

        # fake adt-britney script
        self.adt_britney = os.path.join(self.data.home, 'auto-package-testing',
                                        'jenkins', 'adt-britney')
        os.makedirs(os.path.dirname(self.adt_britney))

        with open(self.adt_britney, 'w') as f:
            f.write('''#!/bin/sh -e
echo "$@" >> /%s/adt-britney.log ''' % self.data.path)
        os.chmod(self.adt_britney, 0o755)

    def tearDown(self):
        del self.data

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

    def run_britney(self, args=[]):
        '''Run britney.

        Assert that it succeeds and does not produce anything on stderr.
        Return (excuses.html, britney_out).
        '''
        britney = subprocess.Popen([self.britney, '-v', '-c', self.britney_conf,
                                    '--distribution=ubuntu',
                                    '--series=%s' % self.data.series],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   cwd=self.data.path,
                                   universal_newlines=True)
        (out, err) = britney.communicate()
        self.assertEqual(britney.returncode, 0, out + err)
        self.assertEqual(err, '')

        with open(os.path.join(self.data.path, 'output', self.data.series,
                               'excuses.html')) as f:
            excuses = f.read()

        return (excuses, out)

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

    def shell(self):
        # uninstallable unstable version
        self.data.add('yellow', True, {'Version': '1.1~beta',
                                       'Depends': 'libc6 (>= 0.9), nosuchpkg'})

        self.make_adt_britney('yellow 1.1~beta RUNNING yellow 1.1~beta\n',
                              'purple 2 FAIL pink 3.0.~britney\n')

        print('run:\n%s -c %s\n' % (self.britney, self.britney_conf))
        subprocess.call(['bash', '-i'], cwd=self.data.path)


unittest.main()
