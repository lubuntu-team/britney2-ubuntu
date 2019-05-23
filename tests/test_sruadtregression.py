#!/usr/bin/python3
# (C) 2018 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import sys
import json
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import DEFAULT, Mock, patch, call
from urllib.request import URLError

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from britney2.policies.policy import PolicyVerdict
from britney2.policies.sruadtregression import SRUADTRegressionPolicy
from tests.test_sourceppa import FakeBritney


FAKE_CHANGES = b"""Format: 1.8
Date: Mon, 16 Jul 2018 17:05:18 -0500
Source: test
Binary: test
Architecture: source
Version: 1.0
Distribution: bionic
Urgency: medium
Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>
Changed-By: Foo Bar <foo.bar@ubuntu.com>
Description:
 test - A test package
Launchpad-Bugs-Fixed: 1 4 2 31337 31337
Changes:
 test (1.0) bionic; urgency=medium
 .
   * Test build
Checksums-Sha1:
 fb11f859b85e09513d395a293dbb0808d61049a7 2454 test_1.0.dsc
 06500cc627bc04a02b912a11417fca5a1109ec97 69852 test_1.0.debian.tar.xz
Checksums-Sha256:
 d91f369a4b7fc4cba63540a81bd6f1492ca86661cf2e3ccac6028fb8c98d5ff5 2454 test_1.0.dsc
 ffb460269ea2acb3db24adb557ba7541fe57fde0b10f6b6d58e8958b9a05b0b9 69852 test_1.0.debian.tar.xz
Files:
 2749eba6cae4e49c00f25e870b228871 2454 libs extra test_1.0.dsc
 95bf4af1ba0766b6f83dd3c3a33b0272 69852 libs extra test_1.0.debian.tar.xz
"""


class FakeOptions:
    distribution = 'testbuntu'
    series = 'zazzy'
    unstable = '/tmp'
    verbose = False
    email_host = 'localhost:1337'


class FakeSourceData:
    version = '55.0'


class FakeExcuse:
    is_valid = True
    daysold = 0
    reason = {'autopkgtest': 1}
    current_policy_verdict = PolicyVerdict.REJECTED_PERMANENTLY


class FakeExcuseRunning:
    is_valid = True
    daysold = 0
    reason = {'autopkgtest': 1}
    current_policy_verdict = PolicyVerdict.REJECTED_TEMPORARILY


class FakeExcusePass:
    is_valid = True
    daysold = 0
    reason = {}
    current_policy_verdict = PolicyVerdict.PASS


class FakeExcuseHinted:
    is_valid = True
    daysold = 0
    reason = {'skiptest': 1}
    current_policy_verdict = PolicyVerdict.PASS_HINTED


class T(unittest.TestCase):

    def setUp(self):
        super().setUp()

    @patch('britney2.policies.sruadtregression.urlopen', return_value=iter(FAKE_CHANGES.split(b'\n')))
    def test_bugs_from_changes(self, urlopen_mock):
        """Check extraction of bug numbers from .changes files"""
        with TemporaryDirectory() as tmpdir:
            options = FakeOptions
            options.unstable = tmpdir
            pol = SRUADTRegressionPolicy(options, {})
            bugs = pol.bugs_from_changes('http://some.url')
            self.assertEqual(len(bugs), 4)
            self.assertSetEqual(bugs, set((1, 4, 2, 31337)))

    @patch('britney2.policies.sruadtregression.urlopen', side_effect=URLError('timeout'))
    def test_bugs_from_changes_retry(self, urlopen_mock):
        """Check .changes extraction retry mechanism"""
        with TemporaryDirectory() as tmpdir:
            options = FakeOptions
            options.unstable = tmpdir
            pol = SRUADTRegressionPolicy(options, {})
            self.assertRaises(URLError, pol.bugs_from_changes, 'http://some.url')
            self.assertEqual(urlopen_mock.call_count, 3)

    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.log')
    @patch('smtplib.SMTP')
    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.bugs_from_changes', return_value={1, 2})
    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.query_lp_rest_api')
    def test_comment_on_regression_and_update_state(self, lp, bugs_from_changes, smtp, log):
        """Verify bug commenting about ADT regressions and save the state"""
        with TemporaryDirectory() as tmpdir:
            options = FakeOptions
            options.unstable = tmpdir

            pkg_mock = {}
            pkg_mock['self_link'] = 'https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/9870565'

            lp.return_value = {'entries': [pkg_mock]}

            previous_state = {
                'testbuntu': {
                    'zazzy': {
                        'testpackage': '54.0',
                        'ignored': '0.1',
                    }
                },
                'ghostdistro': {
                    'spooky': {
                        'ignored': '0.1',
                    }
                }
            }
            pol = SRUADTRegressionPolicy(options, {})
            # Set a base state
            pol.state = previous_state
            status = pol.apply_policy_impl(None, None, 'testpackage', None, FakeSourceData, FakeExcuse)
            self.assertEqual(status, PolicyVerdict.PASS)
            # Assert that we were looking for the right package as per
            # FakeSourceData contents
            self.assertSequenceEqual(lp.mock_calls, [
                call('testbuntu/+archive/primary', {
                    'distro_series': '/testbuntu/zazzy',
                    'exact_match': 'true',
                    'order_by_date': 'true',
                    'pocket': 'Proposed',
                    'source_name': 'testpackage',
                    'version': '55.0',
                    'ws.op': 'getPublishedSources',
                }),
                call('https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/9870565', {
                    'ws.op': 'changesFileUrl',
                })
            ])
            # The .changes file only lists 2 bugs, make sure only those are
            # commented on
            self.assertSequenceEqual(smtp.call_args_list, [
                call('localhost:1337'),
                call('localhost:1337')
            ])
            self.assertEqual(smtp().sendmail.call_count, 2)
            # Check if the state has been saved and not overwritten
            expected_state = {
                'testbuntu': {
                    'zazzy': {
                        'testpackage': '55.0',
                        'ignored': '0.1',
                    }
                },
                'ghostdistro': {
                    'spooky': {
                        'ignored': '0.1',
                    }
                }
            }
            self.assertDictEqual(pol.state, expected_state)
            log.assert_called_with('Sending ADT regression message to LP: #2 regarding testpackage/55.0 in zazzy')

    @patch('smtplib.SMTP')
    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.bugs_from_changes', return_value={1, 2})
    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.query_lp_rest_api')
    def test_no_comment_if_running(self, lp, bugs_from_changes, smtp):
        """Don't comment if tests still running"""
        with TemporaryDirectory() as tmpdir:
            options = FakeOptions
            options.unstable = tmpdir
            pkg_mock = Mock()
            pkg_mock.self_link = 'https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/9870565'

            lp.return_value = {'entries': [pkg_mock]}

            pol = SRUADTRegressionPolicy(options, {})
            status = pol.apply_policy_impl(None, None, 'testpackage', None, FakeSourceData, FakeExcuseRunning)
            self.assertEqual(status, PolicyVerdict.PASS)
            bugs_from_changes.assert_not_called()
            lp.assert_not_called()
            smtp.sendmail.assert_not_called()

    @patch('smtplib.SMTP')
    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.bugs_from_changes', return_value={1, 2})
    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.query_lp_rest_api')
    def test_no_comment_if_passed(self, lp, bugs_from_changes, smtp):
        """Don't comment if all tests passed"""
        with TemporaryDirectory() as tmpdir:
            options = FakeOptions
            options.unstable = tmpdir
            pkg_mock = Mock()
            pkg_mock.self_link = 'https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/9870565'

            bugs_from_changes.return_value = {'entries': [pkg_mock]}

            pol = SRUADTRegressionPolicy(options, {})
            status = pol.apply_policy_impl(None, None, 'testpackage', None, FakeSourceData, FakeExcusePass)
            self.assertEqual(status, PolicyVerdict.PASS)
            bugs_from_changes.assert_not_called()
            lp.assert_not_called()
            smtp.sendmail.assert_not_called()

    @patch('smtplib.SMTP')
    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.bugs_from_changes', return_value={1, 2})
    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.query_lp_rest_api')
    def test_no_comment_if_hinted(self, lp, bugs_from_changes, smtp):
        """Don't comment if package has been hinted in"""
        with TemporaryDirectory() as tmpdir:
            options = FakeOptions
            options.unstable = tmpdir
            pkg_mock = Mock()
            pkg_mock.self_link = 'https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/9870565'
            bugs_from_changes.return_value = {'entries': [pkg_mock]}

            pol = SRUADTRegressionPolicy(options, {})
            status = pol.apply_policy_impl(None, None, 'testpackage', None, FakeSourceData, FakeExcuseHinted)
            self.assertEqual(status, PolicyVerdict.PASS)
            bugs_from_changes.assert_not_called()
            lp.assert_not_called()
            smtp.sendmail.assert_not_called()

    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.query_lp_rest_api')
    def test_initialize(self, lp):
        """Check state load, old package cleanup and LP login"""
        with TemporaryDirectory() as tmpdir:
            options = FakeOptions
            options.unstable = tmpdir
            pkg_mock1 = Mock()
            pkg_mock1.source_name = 'testpackage'
            pkg_mock2 = Mock()
            pkg_mock2.source_name = 'otherpackage'

            # Since we want to be as accurate as possible, we return query
            # results per what query has been performed.
            def query_side_effect(link, query):
                if query['source_name'] == 'testpackage':
                    return {'entries': [pkg_mock1]}
                elif query['source_name'] == 'otherpackage':
                    return {'entries': [pkg_mock2]}
                return {'entries': []}
            lp.side_effect = query_side_effect
            state = {
                'testbuntu': {
                    'zazzy': {
                        'testpackage': '54.0',
                        'toremove': '0.1',
                        'otherpackage': '13ubuntu1',
                    }
                }
            }
            # Prepare the state file
            state_path = os.path.join(
                options.unstable, 'sru_regress_inform_state')
            with open(state_path, 'w') as f:
                json.dump(state, f)
            pol = SRUADTRegressionPolicy(options, {})
            pol.initialise(FakeBritney())
            # Check if the stale packages got purged and others not
            expected_state = {
                'testbuntu': {
                    'zazzy': {
                        'testpackage': '54.0',
                        'otherpackage': '13ubuntu1',
                    }
                },
            }
            # Make sure the state file has been loaded correctly
            self.assertDictEqual(pol.state, expected_state)
            # Check if we logged in with the right LP credentials
            self.assertEqual(pol.email_host, 'localhost:1337')

    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.log')
    @patch('smtplib.SMTP')
    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.bugs_from_changes', return_value={1, 2})
    @patch('britney2.policies.sruadtregression.SRUADTRegressionPolicy.query_lp_rest_api')
    def test_no_comment_dry_run(self, lp, bugs_from_changes, smtp, log):
        """Verify bug commenting about ADT regressions and save the state"""
        with TemporaryDirectory() as tmpdir:
            options = FakeOptions
            options.unstable = tmpdir

            pkg_mock = {}
            pkg_mock['self_link'] = 'https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/9870565'

            lp.return_value = {'entries': [pkg_mock]}

            previous_state = {
                'testbuntu': {
                    'zazzy': {
                        'testpackage': '54.0',
                        'ignored': '0.1',
                    }
                },
                'ghostdistro': {
                    'spooky': {
                        'ignored': '0.1',
                    }
                }
            }
            pol = SRUADTRegressionPolicy(options, {}, dry_run=True)
            # Set a base state
            pol.state = previous_state
            status = pol.apply_policy_impl(None, None, 'testpackage', None, FakeSourceData, FakeExcuse)
            self.assertEqual(status, PolicyVerdict.PASS)
            # Assert that we were looking for the right package as per
            # FakeSourceData contents
            self.assertSequenceEqual(lp.mock_calls, [
                call('testbuntu/+archive/primary', {
                    'distro_series': '/testbuntu/zazzy',
                    'exact_match': 'true',
                    'order_by_date': 'true',
                    'pocket': 'Proposed',
                    'source_name': 'testpackage',
                    'version': '55.0',
                    'ws.op': 'getPublishedSources',
                }),
                call('https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/9870565', {
                    'ws.op': 'changesFileUrl',
                })
            ])

            # Nothing happened
            smtp.assert_not_called()
            smtp.sendmail.assert_not_called()
            self.assertDictEqual(pol.state, previous_state)
            log.assert_called_with('[dry-run] Sending ADT regression message to LP: #2 regarding testpackage/55.0 in zazzy')


if __name__ == '__main__':
    unittest.main()
