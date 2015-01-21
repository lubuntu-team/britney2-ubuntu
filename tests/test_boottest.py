#!/usr/bin/python
# (C) 2014 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import sys
import unittest


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from tests import TestBase


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
            {'Depends': 'libc6 (>= 0.9), libgreen1'})

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
        # `Britney` runs and considers packages for boottesting when
        # it is enabled in the configuration and 'in progress' tests
        # blocks package promotion.
        context = []
        context.append(
            ('green', {'Version': '1.1~beta', 'Depends': 'libc6 (>= 0.9)'}))
        self.do_test(
            context,
            ['<li>boottest for green 1.1~beta: IN PROGRESS',
             '<li>Not considered'])

    def test_pass(self):
        # `Britney` updates boottesting information in excuses when the
        # package test pass and marks the package as a valid candidate for
        # promotion.
        context = []
        context.append(
            ('pyqt5', {'Version': '1.1~beta'}))
        self.do_test(
            context,
            ['<li>boottest for pyqt5 1.1~beta: PASS',
             '<li>Valid candidate'])

    def test_fail(self):
        # `Britney` updates boottesting information in excuses when the
        # package test fails and blocks the package promotion
        # ('Not considered.')
        context = []
        context.append(
            ('pyqt5', {'Version': '1.1'}))
        self.do_test(
            context,
            ['<li>boottest for pyqt5 1.1: FAIL',
             '<li>Not considered'])

    def test_skipped(self):
        # `Britney` updates boottesting information in excuses when the
        # package was skipped and marks the package as a valid candidate for
        # promotion.
        context = []
        context.append(
            ('apache2', {'Version': '2.4.8-1ubuntu1'}))
        self.do_test(
            context,
            ['<li>boottest for apache2 2.4.8-1ubuntu1: SKIPPED',
             '<li>Valid candidate'])


if __name__ == '__main__':
    unittest.main()
