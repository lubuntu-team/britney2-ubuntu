#!/usr/bin/python
# (C) 2014 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import shutil
import sys
import tempfile
import unittest


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from tests import TestBase
from boottest import TouchManifest


def create_manifest(manifest_dir, lines):
    """Helper function for writing touch image manifests."""
    os.makedirs(manifest_dir)
    with open(os.path.join(manifest_dir, 'manifest'), 'w') as fd:
        fd.write('\n'.join(lines))


class TestTouchManifest(unittest.TestCase):

    def setUp(self):
        super(TestTouchManifest, self).setUp()
        self.path = tempfile.mkdtemp(prefix='boottest')
        os.chdir(self.path)
        self.datadir = os.path.join(self.path, 'data/boottest/')
        os.makedirs(self.datadir)
        self.addCleanup(shutil.rmtree, self.path)

    def test_missing(self):
        # Missing manifest file silently results in empty contents.
        manifest = TouchManifest('ubuntu', 'vivid')
        self.assertEqual([], manifest._manifest)
        self.assertNotIn('foo', manifest)

    def test_simple(self):
        # Existing manifest file allows callsites to properly check presence.
        manifest_dir = os.path.join(self.datadir, 'ubuntu/vivid')
        manifest_lines = [
            'bar 1234',
            'foo:armhf       1~beta1',
            'boing1-1.2\t666',
            'click:com.ubuntu.shorts	0.2.346'
        ]
        create_manifest(manifest_dir, manifest_lines)

        manifest = TouchManifest('ubuntu', 'vivid')
        # We can dig deeper on the manifest package names list ...
        self.assertEqual(
            ['bar', 'boing1-1.2', 'foo'], manifest._manifest)
        # but the '<name> in manifest' API reads better.
        self.assertIn('foo', manifest)
        self.assertIn('boing1-1.2', manifest)
        self.assertNotIn('baz', manifest)
        # 'click' name is blacklisted due to the click package syntax.
        self.assertNotIn('click', manifest)


class TestBoottestEnd2End(TestBase):
    """End2End tests (calling `britney`) for the BootTest criteria."""

    def setUp(self):
        super(TestBoottestEnd2End, self).setUp()
        self.britney_conf = os.path.join(
            PROJECT_DIR, 'britney_boottest.conf')
        self.data.add('libc6', False)
        self.data.add(
            'libgreen1',
            False,
            {'Source': 'green', 'Depends': 'libc6 (>= 0.9)'})
        self.data.add(
            'green',
            False,
            {'Source': 'green', 'Depends': 'libc6 (>= 0.9), libgreen1'})
        self.create_manifest([
            'green 1.0',
            'pyqt5:armhf 1.0',
        ])

    def create_manifest(self, lines):
        """Create a manifest for this britney run context."""
        path = os.path.join(
            self.data.path,
            'data/boottest/ubuntu/{}'.format(self.data.series))
        create_manifest(path, lines)

    def do_test(self, context, expect=None, no_expect=None):
        """Process the given package context and assert britney results."""
        for (pkg, fields) in context:
            self.data.add(pkg, True, fields)
        (excuses, out) = self.run_britney()
        #print('-------\nexcuses: %s\n-----' % excuses)
        if expect:
            for re in expect:
                self.assertRegexpMatches(excuses, re)
        if no_expect:
            for re in no_expect:
                self.assertNotRegexpMatches(excuses, re)

    def test_runs(self):
        # `Britney` runs and considers binary packages for boottesting
        # when it is enabled in the configuration, only binaries needed
        # in the phone image are considered for boottesting.
        # 'in progress' tests blocks package promotion.
        context = [
            ('green', {'Source': 'green', 'Version': '1.1~beta',
                       'Depends': 'libc6 (>= 0.9)'}),
            ('libgreen1', {'Source': 'green', 'Version': '1.1~beta',
                           'Depends': 'libc6 (>= 0.9)'}),
        ]
        self.do_test(
            context,
            [r'\bgreen\b.*>1</a> to .*>1.1~beta<',
             '<li>boottest for green 1.1~beta: IN PROGRESS',
             '<li>boottest for libgreen1 1.1~beta: SKIPPED',
             '<li>Not considered'])

    def test_pass(self):
        # `Britney` updates boottesting information in excuses when the
        # package test pass and marks the package as a valid candidate for
        # promotion.
        context = []
        context.append(
            ('pyqt5', {'Source': 'pyqt5-src', 'Version': '1.1~beta'}))
        self.do_test(
            context,
            [r'\bpyqt5-src\b.*\(- to .*>1.1~beta<',
             '<li>boottest for pyqt5 1.1~beta: PASS',
             '<li>Valid candidate'])

    def test_fail(self):
        # `Britney` updates boottesting information in excuses when the
        # package test fails and blocks the package promotion
        # ('Not considered.')
        context = []
        context.append(
            ('pyqt5', {'Source': 'pyqt5-src', 'Version': '1.1'}))
        self.do_test(
            context,
            [r'\bpyqt5-src\b.*\(- to .*>1.1<',
             '<li>boottest for pyqt5 1.1: FAIL',
             '<li>Not considered'])

    def test_skipped(self):
        # `Britney` updates boottesting information in excuses when the
        # package was skipped and marks the package as a valid candidate for
        # promotion.
        context = []
        context.append(
            ('apache2', {'Source': 'apache2-src',
                         'Version': '2.4.8-1ubuntu1'}))
        self.do_test(
            context,
            [r'\bapache2-src\b.*\(- to .*>2.4.8-1ubuntu1<',
             '<li>boottest for apache2 2.4.8-1ubuntu1: SKIPPED',
             '<li>Valid candidate'])


if __name__ == '__main__':
    unittest.main()
