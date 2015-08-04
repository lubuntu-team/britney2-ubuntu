# (C) 2015 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import shutil
import subprocess
import tempfile
import unittest


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

architectures = ['amd64', 'arm64', 'armhf', 'i386', 'powerpc', 'ppc64el']


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
                     True: os.path.join(
                         self.path, 'data', '%s-proposed' % self.series)}
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

    def add(self, name, unstable, fields={}, add_src=True, testsuite=None):
        '''Add a binary package to the index file.

        You need to specify at least the package name and in which list to put
        it (unstable==True for unstable/proposed, or False for
        testing/release). fields specifies all additional entries, e. g.
        {'Depends': 'foo, bar', 'Conflicts: baz'}. There are defaults for most
        fields.

        Unless add_src is set to False, this will also automatically create a
        source record, based on fields['Source'] and name. In that case, the
        "Testsuite:" field is set to the testsuite argument.
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
                srcfields = {'Version': fields['Version'],
                             'Section': fields['Section']}
                if testsuite:
                    srcfields['Testsuite'] = testsuite
                self.add_src(src, unstable, srcfields)

    def add_src(self, name, unstable, fields={}):
        '''Add a source package to the index file.

        You need to specify at least the package name and in which list to put
        it (unstable==True for unstable/proposed, or False for
        testing/release). fields specifies all additional entries, which can be
        Version (default: 1), Section (default: devel), Testsuite (default:
        none), and Extra-Source-Only.
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

    def remove_all(self, unstable):
        '''Remove all added packages'''

        self.added_binaries[unstable] = set()
        self.added_sources[unstable] = set()
        for a in architectures:
            open(os.path.join(self.dirs[unstable], 'Packages_' + a), 'w').close()
        open(os.path.join(self.dirs[unstable], 'Sources'), 'w').close()


class TestBase(unittest.TestCase):

    def setUp(self):
        super(TestBase, self).setUp()
        self.data = TestData()
        self.britney = os.path.join(PROJECT_DIR, 'britney.py')
        # create temporary config so that tests can hack it
        self.britney_conf = os.path.join(self.data.path, 'britney.conf')
        shutil.copy(os.path.join(PROJECT_DIR, 'britney.conf'), self.britney_conf)
        assert os.path.exists(self.britney)

    def tearDown(self):
        del self.data

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

    def create_hint(self, username, content):
        '''Create a hint file for the given username and content'''

        hints_path = os.path.join(
            self.data.path, 'data', self.data.series + '-proposed', 'Hints', username)
        with open(hints_path, 'w') as fd:
            fd.write(content)
