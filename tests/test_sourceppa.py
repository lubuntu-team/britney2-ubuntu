#!/usr/bin/python3
# (C) 2016 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import json
import os
import sys
import unittest
from unittest.mock import DEFAULT, patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from britney2 import Suite, SuiteClass
from britney2.excuse import Excuse
from britney2.hints import HintCollection
from britney2.migrationitem import MigrationItem
from britney2.policies.policy import PolicyEngine, PolicyVerdict
from britney2.policies.sourceppa import LAUNCHPAD_URL, SourcePPAPolicy

# We want to reuse run_it
from tests.test_autopkgtest import TestAutopkgtestBase, tr


CACHE_FILE = os.path.join(PROJECT_DIR, "tests", "data", "sourceppa.json")


class FakeOptions:
    distribution = "testbuntu"
    series = "zazzy"
    unstable = "/tmp"
    verbose = False


class FakeExcuse(Excuse):
    def __init__(self, name, suite):
        self.item = MigrationItem(package=name, version="2.0", suite=suite)
        Excuse.__init__(self, self.item)
        self.policy_verdict = PolicyVerdict.PASS


SOURCE_SUITE = Suite(SuiteClass.PRIMARY_SOURCE_SUITE, "fakename", "fakepath")

PAL = FakeExcuse("pal", SOURCE_SUITE)
BUDDY = FakeExcuse("buddy", SOURCE_SUITE)
FRIEND = FakeExcuse("friend", SOURCE_SUITE)
NOPPA = FakeExcuse("noppa", SOURCE_SUITE)


class FakeBritney:
    def __init__(self):
        self._policy = SourcePPAPolicy(FakeOptions, {})
        self._policy.filename = CACHE_FILE
        self._policy_engine = PolicyEngine()
        self._policy_engine.add_policy(self._policy)
        self._policy_engine.initialise(self, HintCollection())


class FakeData:
    version = "2.0"


class T(unittest.TestCase):
    maxDiff = None

    @patch("britney2.policies.sourceppa.urllib.request.urlopen")
    def test_lp_rest_api_no_entries(self, urlopen):
        """Don't explode if LP reports no entries match pkg/version"""
        context = urlopen.return_value.__enter__.return_value
        context.getcode.return_value = 200
        context.read.return_value = b'{"entries": []}'
        pol = SourcePPAPolicy(FakeOptions, {})
        self.assertEqual(pol.lp_get_source_ppa("hello", "1.0"), "IndexError")

    @patch("britney2.policies.sourceppa.urllib.request.urlopen")
    def test_lp_rest_api_no_source_ppa(self, urlopen):
        """Identify when package has no source PPA"""
        context = urlopen.return_value.__enter__.return_value
        context.getcode.return_value = 200
        context.read.return_value = b'{"entries": [{"self_link": "https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/12345", "build_link": "https://api.launchpad.net/1.0/ubuntu/+source/gcc-5/5.4.1-7ubuntu1/+build/12066956", "other_stuff": "ignored"}]}'
        pol = SourcePPAPolicy(FakeOptions, {})
        self.assertEqual(pol.lp_get_source_ppa("hello", "1.0"), "")

    @patch("britney2.policies.sourceppa.urllib.request.urlopen")
    def test_lp_rest_api_with_source_ppa(self, urlopen):
        """Identify source PPA"""
        context = urlopen.return_value.__enter__.return_value
        context.getcode.return_value = 200
        context.read.return_value = b'{"entries": [{"self_link": "https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/12345", "build_link": "https://api.launchpad.net/1.0/~ci-train-ppa-service/+archive/ubuntu/2516/+build/12063031", "other_stuff": "ignored"}]}'
        pol = SourcePPAPolicy(FakeOptions, {})
        self.assertEqual(
            pol.lp_get_source_ppa("hello", "1.0"),
            "https://api.launchpad.net/1.0/~ci-train-ppa-service/+archive/ubuntu/2516",
        )

    @patch("britney2.policies.sourceppa.urllib.request.urlopen")
    def test_lp_rest_api_errors(self, urlopen):
        """Report errors instead of swallowing them"""
        context = urlopen.return_value.__enter__.return_value
        context.getcode.return_value = 500
        context.read.return_value = b""
        pol = SourcePPAPolicy(FakeOptions, {})
        with self.assertRaisesRegex(ConnectionError, "HTTP 500"):
            pol.lp_get_source_ppa("hello", "1.0")
        # Yes, I have really seen "success with no json returned" in the wild
        context.getcode.return_value = 200
        context.read.return_value = b""
        with self.assertRaisesRegex(ValueError, "Expecting value"):
            pol.lp_get_source_ppa("hello", "1.0")

    @patch("britney2.policies.sourceppa.urllib.request.urlopen")
    def test_lp_rest_api_timeout(self, urlopen):
        """If we get a timeout connecting to LP, we try 5 times"""
        import socket

        # test that we're retried 5 times on timeout
        urlopen.side_effect = socket.timeout
        pol = SourcePPAPolicy(FakeOptions, {})
        with self.assertRaises(socket.timeout):
            pol.lp_get_source_ppa("hello", "1.0")
        self.assertEqual(urlopen.call_count, 5)

    @patch("britney2.policies.sourceppa.urllib.request.urlopen")
    def test_lp_rest_api_unavailable(self, urlopen):
        """If we get a 503 connecting to LP, we try 5 times"""
        from urllib.error import HTTPError

        # test that we're retried 5 times on 503
        urlopen.side_effect = HTTPError(
            None, 503, "Service Temporarily Unavailable", None, None
        )
        pol = SourcePPAPolicy(FakeOptions, {})
        with self.assertRaises(HTTPError):
            pol.lp_get_source_ppa("hello", "1.0")
        self.assertEqual(urlopen.call_count, 5)

    @patch("britney2.policies.sourceppa.urllib.request.urlopen")
    def test_lp_rest_api_flaky(self, urlopen):
        """If we get a 503, then a 200, we get the right result"""
        from urllib.error import HTTPError

        def fail_for_a_bit():
            for i in range(3):
                yield HTTPError(
                    None, 503, "Service Temporarily Unavailable", None, None
                )
            while True:
                yield DEFAULT

        context = urlopen.return_value.__enter__.return_value
        context.getcode.return_value = 200
        context.read.return_value = b'{"entries": [{"self_link": "https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/12345", "build_link": "https://api.launchpad.net/1.0/~ci-train-ppa-service/+archive/ubuntu/2516/+build/12063031", "other_stuff": "ignored"}]}'
        urlopen.side_effect = fail_for_a_bit()
        pol = SourcePPAPolicy(FakeOptions, {})
        pol.lp_get_source_ppa("hello", "1.0")
        self.assertEqual(urlopen.call_count, 5)
        self.assertEqual(
            pol.lp_get_source_ppa("hello", "1.0"),
            "https://api.launchpad.net/1.0/~ci-train-ppa-service/+archive/ubuntu/2516",
        )

    def test_approve_ppa(self):
        """Approve packages by their PPA."""
        shortppa = "~ci-train-ppa-service/+archive/NNNN"
        brit = FakeBritney()
        for excuse in (PAL, BUDDY, FRIEND, NOPPA):
            brit._policy_engine.apply_src_policies(
                excuse.item, FakeData, FakeData, excuse
            )
            self.assertEqual(excuse.policy_verdict, PolicyVerdict.PASS)
        output = FRIEND.policy_info["source-ppa"]
        self.assertDictContainsSubset(
            dict(pal=shortppa, buddy=shortppa, friend=shortppa), output
        )

    def test_ignore_ppa(self):
        """Ignore packages in non-bileto PPAs."""
        shortppa = "~kernel-or-whatever/+archive/ppa"
        brit = FakeBritney()
        for name, versions in brit._policy.cache.items():
            for version in versions:
                brit._policy.cache[name][version] = shortppa
        for excuse in (PAL, BUDDY, FRIEND, NOPPA):
            brit._policy_engine.apply_src_policies(
                excuse.item, FakeData, FakeData, excuse
            )
            self.assertEqual(excuse.policy_verdict, PolicyVerdict.PASS)
        output = FRIEND.policy_info["source-ppa"]
        self.assertEqual(output, {"verdict": "PASS"})

    def test_reject_ppa(self):
        """Reject packages by their PPA."""
        shortppa = "~ci-train-ppa-service/+archive/NNNN"
        brit = FakeBritney()
        excuse = BUDDY
        excuse.policy_verdict = PolicyVerdict.REJECTED_PERMANENTLY
        # Just buddy is invalid but whole ppa fails

        # This one passes because the rejection isn't known yet
        excuse = PAL
        brit._policy_engine.apply_src_policies(
            excuse.item, FakeData, FakeData, excuse
        )
        self.assertEqual(excuse.policy_verdict, PolicyVerdict.PASS)
        # This one fails because it is itself invalid.
        excuse = BUDDY
        brit._policy_engine.apply_src_policies(
            excuse.item, FakeData, FakeData, excuse
        )
        self.assertEqual(
            excuse.policy_verdict, PolicyVerdict.REJECTED_PERMANENTLY
        )
        # This one fails because buddy failed before it.
        excuse = FRIEND
        brit._policy_engine.apply_src_policies(
            excuse.item, FakeData, FakeData, excuse
        )
        self.assertEqual(
            excuse.policy_verdict,
            PolicyVerdict.REJECTED_WAITING_FOR_ANOTHER_ITEM,
        )
        # 'noppa' not from PPA so not rejected
        excuse = NOPPA
        brit._policy_engine.apply_src_policies(
            excuse.item, FakeData, FakeData, excuse
        )
        self.assertEqual(excuse.policy_verdict, PolicyVerdict.PASS)
        # All are rejected however
        for excuse in (PAL, BUDDY, FRIEND):
            self.assertFalse(excuse.is_valid)
        self.assertDictEqual(
            brit._policy.excuses_by_source_ppa,
            {LAUNCHPAD_URL + shortppa: {PAL, BUDDY, FRIEND}},
        )
        output = FRIEND.policy_info["source-ppa"]
        self.assertDictEqual(
            dict(
                pal=shortppa,
                buddy=shortppa,
                friend=shortppa,
                verdict="REJECTED_WAITING_FOR_ANOTHER_ITEM",
            ),
            output,
        )

        output = BUDDY.policy_info["source-ppa"]
        self.assertDictEqual(
            dict(
                pal=shortppa,
                buddy=shortppa,
                friend=shortppa,
                verdict="REJECTED_PERMANENTLY"
            ),
            output,
        )


class AT(TestAutopkgtestBase):
    """ Integration tests for source ppa grouping """

    def test_sourceppa_policy(self):
        """Packages from same source PPA get rejected for failed peer policy"""

        self.data.add_default_packages(green=False)

        ppa = "devel/~ci-train-ppa-service/+archive/NNNN"
        self.sourceppa_cache["green"] = {"2": ppa}
        self.sourceppa_cache["red"] = {"2": ppa}
        with open(
            os.path.join(self.data.path, "data/unstable/Blocks"), "w"
        ) as f:
            f.write("green 12345 1471505000\ndarkgreen 98765 1471500000\n")

        exc = self.run_it(
            [
                ("green", {"Version": "2"}, "autopkgtest"),
                ("red", {"Version": "2"}, "autopkgtest"),
                ("gcc-5", {}, "autopkgtest"),
            ],
            {
                "green": (
                    False,
                    {
                        "green": {
                            "i386": "RUNNING-ALWAYSFAIL",
                            "amd64": "RUNNING-ALWAYSFAIL",
                        }
                    },
                ),
                "red": (
                    False,
                    {
                        "red": {
                            "i386": "RUNNING-ALWAYSFAIL",
                            "amd64": "RUNNING-ALWAYSFAIL",
                        }
                    },
                ),
                "gcc-5": (True, {}),
            },
            {"green": [("reason", "block")], "red": [("reason", "source-ppa")]},
        )[1]
        self.assertEqual(
            exc["red"]["policy_info"]["source-ppa"],
            {
                "red": ppa,
                "green": ppa,
                "verdict": "REJECTED_WAITING_FOR_ANOTHER_ITEM",
            },
        )

        with open(os.path.join(self.data.path, "data/unstable/SourcePPA")) as f:
            res = json.load(f)
            self.assertEqual(
                res,
                {"red": {"2": ppa}, "green": {"2": ppa}, "gcc-5": {"1": ""}},
            )

    def test_sourceppa_missingbuild(self):
        """Packages from same source PPA get rejected for failed peer FTBFS"""

        self.data.add_default_packages(green=False)

        ppa = "devel/~ci-train-ppa-service/+archive/ZZZZ"
        self.sourceppa_cache["green"] = {"2": ppa}
        self.sourceppa_cache["red"] = {"2": ppa}

        self.data.add_src(
            "green", True, {"Version": "2", "Testsuite": "autopkgtest"}
        )
        self.data.add(
            "libgreen1",
            True,
            {"Version": "2", "Source": "green", "Architecture": "i386"},
            add_src=False,
        )
        self.data.add(
            "green",
            True,
            {"Depends": "libc6 (>= 0.9), libgreen1", "Conflicts": "blue"},
            testsuite="autopkgtest",
            add_src=False,
        )

        self.swift.set_results(
            {
                "autopkgtest-testing": {
                    "testing/i386/d/darkgreen/20150101_100000@": (
                        0,
                        "darkgreen 1",
                        tr("green/2"),
                    ),
                    "testing/i386/l/lightgreen/20150101_100100@": (
                        0,
                        "lightgreen 1",
                        tr("green/2"),
                    ),
                    "testing/i386/g/green/20150101_100200@": (
                        0,
                        "green 2",
                        tr("green/2"),
                    ),
                }
            }
        )

        exc = self.run_it(
            [("red", {"Version": "2"}, "autopkgtest")],
            {"green": (False, {}), "red": (False, {})},
            {
                "green": [
                    (
                        "missing-builds",
                        {
                            "on-architectures": [
                                "amd64",
                                "arm64",
                                "armhf",
                                "powerpc",
                                "ppc64el",
                            ],
                            "on-unimportant-architectures": [],
                        },
                    )
                ],
                "red": [("reason", "source-ppa")],
            },
        )[1]
        self.assertEqual(
            exc["red"]["policy_info"]["source-ppa"],
            {
                "red": ppa,
                "green": ppa,
                "verdict": "REJECTED_WAITING_FOR_ANOTHER_ITEM",
            },
        )


if __name__ == "__main__":
    unittest.main()
