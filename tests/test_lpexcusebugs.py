#!/usr/bin/python3
# (C) 2020 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

from collections import defaultdict

import calendar
import fileinput
import json
import os
import pprint
import sys
import time
import unittest
import yaml
from unittest.mock import DEFAULT, patch, call

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from britney2.policies.policy import PolicyVerdict
from britney2.policies.email import EmailPolicy, person_chooser, address_chooser

from tests.test_sourceppa import FakeOptions
from tests import TestBase


class FakeItem:
    package = "chromium-browser"


class FakeSourceData:
    version = "55.0"


FakeItem,


class FakeExcuse:
    is_valid = True
    daysold = 0
    reason = []
    tentative_policy_verdict = PolicyVerdict.PASS


class ET(TestBase):
    """ Test block bug policy """

    def setUp(self):
        super().setUp()
        # disable ADT, not relevant for us
        for line in fileinput.input(self.britney_conf, inplace=True):
            if line.startswith("ADT_ENABLE"):
                print("ADT_ENABLE = no")
            elif line.startswith("MINDAYS_EMERGENCY"):
                print("MINDAYS_EMERGENCY = 10")
            elif not line.startswith("ADT_AMQP") and not line.startswith(
                "ADT_SWIFT_URL"
            ):
                sys.stdout.write(line)
        self.excuse_bugs_file = os.path.join(self.data.dirs[True], "ExcuseBugs")
        self.sourceppa_cache = {}

        self.data.add("libc6", False)

    def do_test(self, unstable_add, expect_bugs):
        """Run britney with some unstable packages and verify excuses.

        unstable_add is a list of (binpkgname, field_dict, [(bugno, timestamp)])

        expect_bugs is a list of tuples (package, bug, timestamp) that is
        checked against the bugs reported as being blocked during this
        do_test run.

        Return (output, excuses_dict, excuses_html, bugs).
        """
        for (pkg, fields, bugs) in unstable_add:
            self.data.add(pkg, True, fields, True, None)
            self.sourceppa_cache.setdefault(pkg, {})
            if fields["Version"] not in self.sourceppa_cache[pkg]:
                self.sourceppa_cache[pkg][fields["Version"]] = ""
            print("Writing to %s" % self.excuse_bugs_file)
            with open(self.excuse_bugs_file, "w") as f:
                for (bug, ts) in bugs:
                    f.write("%s %s %s" % (pkg, bug, ts))

        # Set up sourceppa cache for testing
        sourceppa_path = os.path.join(self.data.dirs[True], "SourcePPA")
        with open(sourceppa_path, "w", encoding="utf-8") as sourceppa:
            sourceppa.write(json.dumps(self.sourceppa_cache))

        (excuses_yaml, excuses_html, out) = self.run_britney()

        bugs_blocked_by = []

        # convert excuses to source indexed dict
        excuses_dict = {}
        for s in yaml.safe_load(excuses_yaml)["sources"]:
            excuses_dict[s["source"]] = s

        if "SHOW_EXCUSES" in os.environ:
            print("------- excuses -----")
            pprint.pprint(excuses_dict, width=200)
        if "SHOW_HTML" in os.environ:
            print("------- excuses.html -----\n%s\n" % excuses_html)
        if "SHOW_OUTPUT" in os.environ:
            print("------- output -----\n%s\n" % out)

        self.assertNotIn("FIXME", out)

        self.assertDictEqual(
            expect_bugs["libc6"],
            {
                k: v
                for (k, v) in excuses_dict["libc6"]["policy_info"][
                    "update-excuse"
                ].items()
                if k != "verdict"
            },
        )

        return (out, excuses_dict, excuses_html, bugs_blocked_by)

    def test_email_sent(self):
        """Test that an email is sent through the SMTP server"""
        unixtime = calendar.timegm(time.strptime("Thu May 28 16:38:19 2020"))
        pkg = ("libc6", {"Version": "2"}, [("1", unixtime)])

        self.do_test([pkg], {"libc6": {"1": unixtime}})


if __name__ == "__main__":
    unittest.main()
