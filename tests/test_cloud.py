#!/usr/bin/python3
# (C) 2022 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from britney2.policies.cloud import CloudPolicy, ERR_MESSAGE, MissingURNException

class FakeItem:
    package = "chromium-browser"
    version = "0.0.1"

class FakeSourceData:
    version = "55.0"

class T(unittest.TestCase):
    def setUp(self):
        self.fake_options = SimpleNamespace(
            distrubtion = "testbuntu",
            series = "zazzy",
            unstable = "/tmp",
            verbose = False,
            cloud_source = "zazzy-proposed",
            cloud_source_type = "archive",
            cloud_azure_zazzy_urn = "fake-urn-value",
            cloud_state_file = "/tmp/test_state.json"
        )
        self.policy = CloudPolicy(self.fake_options, {})
        self.policy._setup_work_directory()

    def tearDown(self):
        self.policy._cleanup_work_directory()

    @patch("britney2.policies.cloud.CloudPolicy._store_extra_test_result_info")
    @patch("britney2.policies.cloud.CloudPolicy._parse_xunit_test_results")
    @patch("subprocess.run")
    def test_run_cloud_tests_state_handling(self, mock_run, mock_xunit, mock_extra):
        """Cloud tests should save state and not re-run tests for packages
           already tested."""
        expected_state = {
            "azure": {
                "archive": {
                    "zazzy": {
                        "chromium-browser": {
                            "version": "55.0",
                            "failures": 1,
                            "errors": 1,
                        }
                    }
                }
            }
        }
        with open(self.policy.options.cloud_state_file, "w") as file:
            json.dump(expected_state, file)
        self.policy._load_state()

        # Package already tested, no tests should run
        self.policy.failures = {}
        self.policy.errors = {}
        self.policy._run_cloud_tests("chromium-browser", "55.0", "zazzy", ["proposed"], "archive")
        self.assertDictEqual(expected_state, self.policy.state)
        mock_run.assert_not_called()
        self.assertEqual(len(self.policy.failures), 1)
        self.assertEqual(len(self.policy.errors), 1)

        # A new package appears, tests should run
        expected_state["azure"]["archive"]["zazzy"]["hello"] = {
            "version": "2.10",
            "failures": 0,
            "errors": 0,
        }
        self.policy.failures = {}
        self.policy.errors = {}
        self.policy._run_cloud_tests("hello", "2.10", "zazzy", ["proposed"], "archive")
        self.assertDictEqual(expected_state, self.policy.state)
        mock_run.assert_called()
        self.assertEqual(len(self.policy.failures), 0)
        self.assertEqual(len(self.policy.errors), 0)

        # A new version of existing package, tests should run
        expected_state["azure"]["archive"]["zazzy"]["chromium-browser"] = {
            "version": "55.1",
            "failures": 0,
            "errors": 0,
        }
        self.policy.failures = {}
        self.policy.errors = {}
        self.policy._run_cloud_tests("chromium-browser", "55.1", "zazzy", ["proposed"], "archive")
        self.assertDictEqual(expected_state, self.policy.state)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(len(self.policy.failures), 0)
        self.assertEqual(len(self.policy.errors), 0)

        # Make sure the state was saved properly
        with open(self.policy.options.cloud_state_file, "r") as file:
            self.assertDictEqual(expected_state, json.load(file))

    @patch("britney2.policies.cloud.CloudPolicy._store_extra_test_result_info")
    @patch("britney2.policies.cloud.CloudPolicy._parse_xunit_test_results")
    @patch("subprocess.run")
    def test_run_cloud_tests_state_handling_only_errors(self, mock_run, mock_xunit, mock_extra):
        """Cloud tests should save state and not re-run tests for packages
           already tested."""
        start_state = {
            "azure": {
                "archive": {
                    "zazzy": {
                        "chromium-browser": {
                            "version": "55.0",
                            "failures": 0,
                            "errors": 2,
                        }
                    }
                }
            }
        }
        with open(self.policy.options.cloud_state_file, "w") as file:
            json.dump(start_state, file)
        self.policy._load_state()

        # Package already tested, but only had errors - rerun
        self.policy._run_cloud_tests("chromium-browser", "55.0", "zazzy", ["proposed"], "archive")
        mock_run.assert_called()

    @patch("britney2.policies.cloud.CloudPolicy._run_cloud_tests")
    def test_run_cloud_tests_called_for_package_in_manifest(self, mock_run):
        """Cloud tests should run for a package in the cloud package set.
        """
        self.policy.package_set = set(["chromium-browser"])
        self.policy.options.series = "jammy"

        self.policy.apply_src_policy_impl(
            None, FakeItem, None, FakeSourceData, None
        )

        mock_run.assert_called_once_with(
            "chromium-browser", "55.0", "jammy", ["proposed"], "archive"
        )

    @patch("britney2.policies.cloud.CloudPolicy._run_cloud_tests")
    def test_run_cloud_tests_not_called_for_package_not_in_manifest(self, mock_run):
        """Cloud tests should not run for packages not in the cloud package set"""

        self.policy.package_set = set(["vim"])
        self.policy.options.series = "jammy"

        self.policy.apply_src_policy_impl(
            None, FakeItem, None, FakeSourceData, None
        )

        mock_run.assert_not_called()

    @patch("britney2.policies.cloud.smtplib")
    @patch("britney2.policies.cloud.CloudPolicy._run_cloud_tests")
    def test_no_tests_run_during_dry_run(self, mock_run, smtp):
        self.policy = CloudPolicy(self.fake_options, {}, dry_run=True)
        self.policy.package_set = set(["chromium-browser"])
        self.policy.options.series = "jammy"
        self.policy.source = "jammy-proposed"

        self.policy.apply_src_policy_impl(
            None, FakeItem, None, FakeSourceData, None
        )

        mock_run.assert_not_called()
        self.assertEqual(smtp.mock_calls, [])

    def test_finding_results_file(self):
        """Ensure result file output from Cloud Test Framework can be found"""
        path = os.path.join(self.policy.work_dir, "TEST-FakeTests-20230101010101.xml")
        path2 = os.path.join(self.policy.work_dir, "Test-OtherTests-20230101010101.xml")
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
        self.policy.options.series = "jammy"
        self.policy.source = "jammy-proposed"
        message = self.policy._format_email_message(ERR_MESSAGE, ["work@canonical.com"], "vim", "9.0", failures)

        self.assertIn("To: work@canonical.com", message)
        self.assertIn("vim 9.0", message)
        self.assertIn("Error reason 2", message)

    def test_urn_retrieval(self):
        """Test that URN retrieval throws the expected error when not configured."""
        self.assertRaises(
            MissingURNException, self.policy._retrieve_urn, "jammy"
        )

        urn = self.policy._retrieve_urn("zazzy")
        self.assertEqual(urn, "fake-urn-value")

    def test_generation_of_verdict_info(self):
        """Test that the verdict info correctly states which clouds had failures and/or errors"""
        failures = {
            "cloud1": {
                "test_name1": "message1",
                "test_name2": "message2"
            },
            "cloud2": {
                "test_name3": "message3"
            }
        }

        errors = {
            "cloud1": {
                "test_name4": "message4",
            },
            "cloud3": {
                "test_name5": "message5"
            }
        }

        info = self.policy._generate_verdict_info(failures, errors)

        expected_failure_info = "Cloud testing failed for cloud1,cloud2."
        expected_error_info = "Cloud testing had errors for cloud1,cloud3."

        self.assertIn(expected_failure_info, info)
        self.assertIn(expected_error_info, info)

    def test_format_install_flags_with_ppas(self):
        """Ensure the correct flags are returned with PPA sources"""
        expected_flags = [
            "--install-ppa-package", "tmux/ppa_url=fingerprint",
            "--install-ppa-package", "tmux/ppa_url2=fingerprint"
        ]
        install_flags = self.policy._format_install_flags(
            "tmux", ["ppa_url=fingerprint", "ppa_url2=fingerprint"], "ppa"
        )

        self.assertListEqual(install_flags, expected_flags)

    def test_format_install_flags_with_archive(self):
        """Ensure the correct flags are returned with archive sources"""
        expected_flags = ["--install-archive-package", "tmux/proposed"]
        install_flags = self.policy._format_install_flags("tmux", ["proposed"], "archive")

        self.assertListEqual(install_flags, expected_flags)

    def test_format_install_flags_with_incorrect_type(self):
        """Ensure errors are raised for unknown source types"""

        self.assertRaises(RuntimeError, self.policy._format_install_flags, "tmux", ["a_source"], "something")

    def test_parse_ppas(self):
        """Ensure correct conversion from Britney format to cloud test format
        Also check that public PPAs are not used due to fingerprint requirement for cloud
        tests.
        """
        input_ppas = [
            "deadsnakes/ppa:fingerprint",
            "user:token@team/name:fingerprint"
        ]

        expected_ppas = [
            "https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu=fingerprint",
            "https://user:token@private-ppa.launchpadcontent.net/team/name/ubuntu=fingerprint"
        ]

        output_ppas = self.policy._parse_ppas(input_ppas)
        self.assertListEqual(output_ppas, expected_ppas)

    def test_errors_raised_if_invalid_ppa_input(self):
        """Test that error are raised if input PPAs don't match expected format"""
        self.assertRaises(
            RuntimeError, self.policy._parse_ppas, ["team/name"]
        )

        self.assertRaises(
            RuntimeError, self.policy._parse_ppas, ["user:token@team/name"]
        )

        self.assertRaises(
            RuntimeError, self.policy._parse_ppas, ["user:token@team=fingerprint"]
        )

    def test_retrieve_package_install_source_from_test_output(self):
        """Ensure retrieving the package install source from apt output only returns the line we
        want and not other lines containing the package name.

        Ensure it returns nothing if multiple candidates are found because that means the parsing
        needs to be updated.
        """
        package = "tmux"

        with open(os.path.join(self.policy.work_dir, self.policy.TEST_LOG_FILE), "w") as file:
            file.write("Get: something \n".format(package))
            file.write("Get: lib-{} \n".format(package))

        install_source = self.policy._retrieve_package_install_source_from_test_output(package)
        self.assertIsNone(install_source)

        with open(os.path.join(self.policy.work_dir, self.policy.TEST_LOG_FILE), "a") as file:
            file.write("Get: {} \n".format(package))

        install_source = self.policy._retrieve_package_install_source_from_test_output(package)
        self.assertEqual(install_source, "Get: tmux \n")

    @patch("britney2.policies.cloud.CloudPolicy._retrieve_package_install_source_from_test_output")
    def test_store_extra_test_result_info(self, mock):
        """Ensure nothing is done if there are no failures/errors.
        Ensure that if there are failures/errors that any extra info retrieved is stored in the
        results dict Results -> Cloud -> extra_info
        """
        self.policy._store_extra_test_result_info("FakeCloud", "tmux")
        mock.assert_not_called()

        self.policy.failures = {"FakeCloud": {"failing_test": "failure reason"}}
        mock.return_value = "source information"
        self.policy._store_extra_test_result_info("FakeCloud", "tmux")
        self.assertEqual(
            self.policy.failures["FakeCloud"]["extra_info"]["install_source"], "source information"
        )

    def _create_fake_test_result_file(self, num_pass=1, num_err=0, num_fail=0):
        """Helper function to generate an xunit test result file.

        :param num_pass The number of passing tests to include
        :param num_err The number of erroring tests to include
        :param num_fail The number of failing tests to include

        Returns the path to the created file.
        """
        os.makedirs(self.policy.work_dir, exist_ok=True)
        path = os.path.join(self.policy.work_dir, "TEST-FakeTests-20230101010101.xml")

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
