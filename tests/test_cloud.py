#!/usr/bin/python3
# (C) 2022 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import json
import os
import pathlib
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from britney2.policies.cloud import CloudPolicy, ERR_MESSAGE
from tests.test_sourceppa import FakeOptions

class FakeItem:
    package = "chromium-browser"
    version = "0.0.1"

class FakeSourceData:
    version = "55.0"

class T(unittest.TestCase):
    def setUp(self):
        self.policy = CloudPolicy(FakeOptions, {})
        self.policy._setup_work_directory()

    def tearDown(self):
        self.policy._cleanup_work_directory()

    def test_retrieve_series_and_pocket_from_path(self):
        """Retrieves the series and pocket from the suite path.
        Ensure an exception is raised if the regex fails to match.
        """
        result = self.policy._retrieve_series_and_pocket_from_path("data/jammy-proposed")
        self.assertTupleEqual(result, ("jammy", "proposed"))

        self.assertRaises(
            RuntimeError, self.policy._retrieve_series_and_pocket_from_path, "data/badpath"
        )

    @patch("britney2.policies.cloud.CloudPolicy._run_cloud_tests")
    def test_run_cloud_tests_called_for_package_in_manifest(self, mock_run):
        """Cloud tests should run for a package in the cloud package set.
        """
        self.policy.package_set = set(["chromium-browser"])
        self.policy.series = "jammy"
        self.policy.pocket = "proposed"

        self.policy.apply_src_policy_impl(
            None, FakeItem, None, FakeSourceData, None
        )

        mock_run.assert_called_once_with("chromium-browser", "jammy", "proposed")

    @patch("britney2.policies.cloud.CloudPolicy._run_cloud_tests")
    def test_run_cloud_tests_not_called_for_package_not_in_manifest(self, mock_run):
        """Cloud tests should not run for packages not in the cloud package set"""

        self.policy.package_set = set(["vim"])
        self.policy.series = "jammy"
        self.policy.pocket = "proposed"

        self.policy.apply_src_policy_impl(
            None, FakeItem, None, FakeSourceData, None
        )

        mock_run.assert_not_called()

    @patch("britney2.policies.cloud.smtplib")
    @patch("britney2.policies.cloud.CloudPolicy._run_cloud_tests")
    def test_no_tests_run_during_dry_run(self, mock_run, smtp):
        self.policy = CloudPolicy(FakeOptions, {}, dry_run=True)
        self.policy.package_set = set(["chromium-browser"])
        self.policy.series = "jammy"
        self.policy.pocket = "proposed"

        self.policy.apply_src_policy_impl(
            None, FakeItem, None, FakeSourceData, None
        )

        mock_run.assert_not_called()
        self.assertEqual(smtp.mock_calls, [])

    def test_finding_results_file(self):
        """Ensure result file output from Cloud Test Framework can be found"""
        path = pathlib.PurePath(CloudPolicy.WORK_DIR, "TEST-FakeTests-20230101010101.xml")
        path2 = pathlib.PurePath(CloudPolicy.WORK_DIR, "Test-OtherTests-20230101010101.xml")
        with open(path, "a"): pass
        with open(path2, "a"): pass

        regex = r"TEST-FakeTests-[0-9]*.xml"
        results_file_paths = self.policy._find_results_files(regex)

        self.assertEqual(len(results_file_paths), 1)
        self.assertEqual(results_file_paths[0], path)

    def test_parsing_of_xunit_results_file(self):
        """Test that parser correctly sorts and stores test failures and errors"""
        path = self._create_fake_test_result_file(num_pass=4, num_err=2, num_fail=3)
        self.policy._parse_xunit_test_results("Azure", [path])

        azure_failures = self.policy.failures.get("Azure", {})
        azure_errors = self.policy.errors.get("Azure", {})

        self.assertEqual(len(azure_failures), 3)
        self.assertEqual(len(azure_errors), 2)

        test_names = azure_failures.keys()
        self.assertIn("failing_test_1", test_names)

        self.assertEqual(
            azure_failures.get("failing_test_1"), "AssertionError: A useful error message"
        )

    def test_email_formatting(self):
        """Test that information is inserted correctly in the email template"""
        failures = {
            "Azure": {
                "failing_test1": "Error reason 1",
                "failing_test2": "Error reason 2"
            }
        }
        self.policy.series = "jammy"
        self.policy.pocket = "proposed"
        message = self.policy._format_email_message(ERR_MESSAGE, ["work@canonical.com"], "vim", "9.0", failures)

        self.assertIn("To: work@canonical.com", message)
        self.assertIn("vim 9.0", message)
        self.assertIn("Error reason 2", message)

    def _create_fake_test_result_file(self, num_pass=1, num_err=0, num_fail=0):
        """Helper function to generate an xunit test result file.

        :param num_pass The number of passing tests to include
        :param num_err The number of erroring tests to include
        :param num_fail The number of failing tests to include

        Returns the path to the created file.
        """
        os.makedirs(CloudPolicy.WORK_DIR, exist_ok=True)
        path = pathlib.PurePath(CloudPolicy.WORK_DIR, "TEST-FakeTests-20230101010101.xml")

        root = ET.Element("testsuite", attrib={"name": "FakeTests-1234567890"})

        for x in range(0, num_pass):
            case_attrib = {"classname": "FakeTests", "name": "passing_test_{}".format(x), "time":"0.001"}
            ET.SubElement(root, "testcase", attrib=case_attrib)

        for x in range(0, num_err):
            case_attrib = {"classname": "FakeTests", "name": "erroring_test_{}".format(x), "time":"0.001"}
            testcase = ET.SubElement(root, "testcase", attrib=case_attrib)

            err_attrib = {"type": "Exception", "message": "A useful error message" }
            ET.SubElement(testcase, "error", attrib=err_attrib)

        for x in range(0, num_fail):
            case_attrib = {"classname": "FakeTests", "name": "failing_test_{}".format(x), "time":"0.001"}
            testcase = ET.SubElement(root, "testcase", attrib=case_attrib)

            fail_attrib = {"type": "AssertionError", "message": "A useful error message" }
            ET.SubElement(testcase, "failure", attrib=fail_attrib)


        tree = ET.ElementTree(root)
        ET.indent(tree, space="\t", level=0)

        with open(path, "w") as file:
            tree.write(file, encoding="unicode", xml_declaration=True)

        return path

if __name__ == "__main__":
    unittest.main()
