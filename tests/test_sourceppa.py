#!/usr/bin/python3
# (C) 2016 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from policies.policy import PolicyVerdict
from policies.sourceppa import LAUNCHPAD_URL, SourcePPAPolicy


CACHE_FILE = os.path.join(PROJECT_DIR, 'tests', 'data', 'sourceppa.json')


class FakeOptions:
    distribution = 'testbuntu'
    series = 'zazzy'
    unstable = '/tmp'
    verbose = False


class FakeExcuse:
    ver = ('1.0', '2.0')
    is_valid = True
    policy_info = {}

    def addreason(self, reason):
        """Ignore reasons."""

    def addhtml(self, reason):
        """Ignore reasons."""


class FakeBritney:
    def __init__(self):
        self.excuses = dict(
            pal=FakeExcuse(),
            buddy=FakeExcuse(),
            friend=FakeExcuse(),
            noppa=FakeExcuse())


class FakeData:
    version = '2.0'


class T(unittest.TestCase):
    maxDiff = None

    @patch('policies.sourceppa.urllib.request.urlopen')
    def test_lp_rest_api_no_entries(self, urlopen):
        """Don't explode if LP reports no entries match pkg/version"""
        context = urlopen.return_value.__enter__.return_value
        context.getcode.return_value = 200
        context.read.return_value = b'{"entries": []}'
        pol = SourcePPAPolicy(FakeOptions)
        self.assertEqual(pol.lp_get_source_ppa('hello', '1.0'), '')

    @patch('policies.sourceppa.urllib.request.urlopen')
    def test_lp_rest_api_no_source_ppa(self, urlopen):
        """Identify when package has no source PPA"""
        context = urlopen.return_value.__enter__.return_value
        context.getcode.return_value = 200
        context.read.return_value = b'{"entries": [{"copy_source_archive_link": null, "other_stuff": "ignored"}]}'
        pol = SourcePPAPolicy(FakeOptions)
        self.assertEqual(pol.lp_get_source_ppa('hello', '1.0'), '')

    @patch('policies.sourceppa.urllib.request.urlopen')
    def test_lp_rest_api_with_source_ppa(self, urlopen):
        """Identify source PPA"""
        context = urlopen.return_value.__enter__.return_value
        context.getcode.return_value = 200
        context.read.return_value = b'{"entries": [{"copy_source_archive_link": "https://api.launchpad.net/1.0/team/ubuntu/ppa", "other_stuff": "ignored"}]}'
        pol = SourcePPAPolicy(FakeOptions)
        self.assertEqual(pol.lp_get_source_ppa('hello', '1.0'), 'https://api.launchpad.net/1.0/team/ubuntu/ppa')

    @patch('policies.sourceppa.urllib.request.urlopen')
    def test_lp_rest_api_errors(self, urlopen):
        """Report errors instead of swallowing them"""
        context = urlopen.return_value.__enter__.return_value
        context.getcode.return_value = 500
        context.read.return_value = b''
        pol = SourcePPAPolicy(FakeOptions)
        with self.assertRaisesRegex(ConnectionError, 'HTTP 500'):
            pol.lp_get_source_ppa('hello', '1.0')
        # Yes, I have really seen "success with no json returned" in the wild
        context.getcode.return_value = 200
        context.read.return_value = b''
        with self.assertRaisesRegex(ValueError, 'Expecting value'):
            pol.lp_get_source_ppa('hello', '1.0')

    def test_approve_ppa(self):
        """Approve packages by their PPA."""
        shortppa = 'team/ubuntu/ppa'
        pol = SourcePPAPolicy(FakeOptions)
        pol.filename = CACHE_FILE
        pol.initialise(FakeBritney())
        output = {}
        for pkg in ('pal', 'buddy', 'friend', 'noppa'):
            self.assertEqual(pol.apply_policy_impl(output, None, pkg, None, FakeData, FakeExcuse), PolicyVerdict.PASS)
        self.assertEqual(output, dict(pal=shortppa, buddy=shortppa, friend=shortppa))

    def test_reject_ppa(self):
        """Reject packages by their PPA."""
        shortppa = 'team/ubuntu/ppa'
        pol = SourcePPAPolicy(FakeOptions)
        pol.filename = CACHE_FILE
        brit = FakeBritney()
        brit.excuses['buddy'].is_valid = False  # Just buddy is invalid but whole ppa fails
        pol.initialise(brit)
        output = {}
        # This one passes because the rejection isn't known yet
        self.assertEqual(pol.apply_policy_impl(output, None, 'pal', None, FakeData, brit.excuses['pal']), PolicyVerdict.PASS)
        # This one fails because it is itself invalid.
        self.assertEqual(pol.apply_policy_impl(output, None, 'buddy', None, FakeData, brit.excuses['buddy']), PolicyVerdict.REJECTED_PERMANENTLY)
        # This one fails because buddy failed before it.
        self.assertEqual(pol.apply_policy_impl(output, None, 'friend', None, FakeData, brit.excuses['friend']), PolicyVerdict.REJECTED_PERMANENTLY)
        # 'noppa' not from PPA so not rejected
        self.assertEqual(pol.apply_policy_impl(output, None, 'noppa', None, FakeData, FakeExcuse), PolicyVerdict.PASS)
        # All are rejected however
        for pkg in ('pal', 'buddy', 'friend'):
            self.assertFalse(brit.excuses[pkg].is_valid)
        self.assertDictEqual(pol.pkgs_by_source_ppa, {
            LAUNCHPAD_URL + shortppa: {'pal', 'buddy', 'friend'}})
        self.assertEqual(output, dict(pal=shortppa, buddy=shortppa, friend=shortppa))


if __name__ == '__main__':
    unittest.main()
