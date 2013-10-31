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
# We want to reuse run_it
from tests.test_autopkgtest import TestAutopkgtestBase


class T(TestAutopkgtestBase):
    def test_lp_bug_block(self):
        self.data.add_default_packages(darkgreen=False)

        with open(
            os.path.join(self.data.path, "data/unstable/Blocks"), "w"
        ) as f:
            f.write("darkgreen 12345 1471505000\ndarkgreen 98765 1471500000\n")

        exc = self.run_it(
            [("darkgreen", {"Version": "2"}, "autopkgtest")],
            {
                "darkgreen": (
                    False,
                    {
                        "darkgreen": {
                            "i386": "RUNNING-ALWAYSFAIL",
                            "amd64": "RUNNING-ALWAYSFAIL",
                        }
                    },
                )
            },
            {
                "darkgreen": [
                    ("reason", "block"),
                    (
                        "excuses",
                        'Not touching package as requested in <a href="https://launchpad.net/bugs/12345">bug 12345</a> on Thu Aug 18 07:23:20 2016',
                    ),
                    ("is-candidate", False),
                ]
            },
        )[1]
        self.assertEqual(
            exc["darkgreen"]["policy_info"]["block-bugs"],
            {
                "12345": 1471505000,
                "98765": 1471500000,
                "verdict": "REJECTED_PERMANENTLY",
            },
        )
