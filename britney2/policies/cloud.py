import json
import os
from pathlib import PurePath
import re
import shutil
import smtplib
import socket
import subprocess
import xunitparser

from britney2 import SuiteClass
from britney2.policies.policy import BasePolicy
from britney2.policies import PolicyVerdict

FAIL_MESSAGE = """From: Ubuntu Release Team <noreply+proposed-migration@ubuntu.com>
To: {recipients}
X-Proposed-Migration: notice
Subject: [proposed-migration] {package} {version} in {series}-{pocket} failed Cloud tests.

Hi,

{package} {version} needs attention.

This package fails the following tests:

{results}

If you have any questions about this email, please ask them in #ubuntu-release channel on libera.chat.

Regards, Ubuntu Release Team.
"""

ERR_MESSAGE = """From: Ubuntu Release Team <noreply+proposed-migration@ubuntu.com>
To: {recipients}
X-Proposed-Migration: notice
Subject: [proposed-migration] {package} {version} in {series}-{pocket} had errors running Cloud Tests.

Hi,

During Cloud tests of {package} {version} the following errors occurred:

{results}

If you have any questions about this email, please ask them in #ubuntu-release channel on libera.chat.

Regards, Ubuntu Release Team.
"""
class CloudPolicy(BasePolicy):
    WORK_DIR = "cloud_tests"
    PACKAGE_SET_FILE = "cloud_package_set"
    EMAILS = ["cpc@canonical.com"]
    SERIES_TO_URN = {
        "lunar": "Canonical:0001-com-ubuntu-server-lunar-daily:23_04-daily-gen2:23.04.202301030",
        "kinetic": "Canonical:0001-com-ubuntu-server-kinetic:22_10:22.10.202301040",
        "jammy": "Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:22.04.202212140",
        "focal": "Canonical:0001-com-ubuntu-server-focal:20_04-lts-gen2:20.04.202212140",
        "bionic": "Canonical:UbuntuServer:18_04-lts-gen2:18.04.202212090"
    }

    def __init__(self, options, suite_info, dry_run=False):
        super().__init__(
            "cloud", options, suite_info, {SuiteClass.PRIMARY_SOURCE_SUITE}
        )
        self.dry_run = dry_run
        if self.dry_run:
            self.logger.info("Cloud Policy: Dry-run enabled")

        self.email_host = getattr(self.options, "email_host", "localhost")
        self.logger.info(
            "Cloud Policy: will send emails to: %s", self.email_host
        )
        self.failures = {}
        self.errors = {}

    def initialise(self, britney):
        super().initialise(britney)

        primary_suite = self.suite_info.primary_source_suite
        series, pocket = self._retrieve_series_and_pocket_from_path(primary_suite.path)
        self.series = series
        self.pocket = pocket
        self.package_set = self._retrieve_cloud_package_set_for_series(self.series)

    def apply_src_policy_impl(self, policy_info, item, source_data_tdist, source_data_srcdist, excuse):
        if item.package not in self.package_set:
            return PolicyVerdict.PASS

        if self.dry_run:
            self.logger.info(
                "Cloud Policy: Dry run would test {} in {}-{}".format(item.package , self.series, self.pocket)
            )
            return PolicyVerdict.PASS

        self._setup_work_directory()
        self.failures = {}
        self.errors = {}

        self._run_cloud_tests(item.package, self.series, self.pocket)
        self._send_emails_if_needed(item.package, source_data_srcdist.version, self.series, self.pocket)

        self._cleanup_work_directory()
        return PolicyVerdict.PASS

    def _retrieve_cloud_package_set_for_series(self, series):
        """Retrieves a set of packages for the given series in which cloud
        tests should be run.

        Temporarily a static list retrieved from file. Will be updated to
        retrieve from a database at a later date.

        :param series The Ubuntu codename for the series (e.g. jammy)
        """
        package_set = set()

        with open(self.PACKAGE_SET_FILE) as file:
            for line in file:
                package_set.add(line.strip())

        return package_set

    def _retrieve_series_and_pocket_from_path(self, suite_path):
        """Given the suite path return a tuple of series and pocket.
        With input 'data/jammy-proposed' the expected output is a tuple of
        (jammy, proposed)

        :param suite_path The path attribute of the suite
        """
        result = (None, None)
        match = re.search("([a-z]*)-([a-z]*)$", suite_path)

        if(match):
            result = match.groups()
        else:
            raise RuntimeError(
                "Could not determine series and pocket from the path: %s" %suite_path
            )
        return result

    def _run_cloud_tests(self, package, series, pocket):
        """Runs any cloud tests for the given package.
        Returns a list of failed tests or an empty list if everything passed.

        :param package The name of the package to test
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param pocket The name of the pocket the package should be installed from (e.g. proposed)
        """
        self._run_azure_tests(package, series, pocket)

    def _send_emails_if_needed(self, package, version, series, pocket):
        """Sends email(s) if there are test failures and/or errors

        :param package The name of the package that was tested
        :param version The version number of the package
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param pocket The name of the pocket the package should be installed from (e.g. proposed)
        """
        if len(self.failures) > 0:
            emails = self.EMAILS
            message = self._format_email_message(
                FAIL_MESSAGE, emails, package, version, self.failures
            )
            self.logger.info("Cloud Policy: Sending failure email for {}, to {}".format(package, emails))
            self._send_email(emails, message)

        if len(self.errors) > 0:
            emails = self.EMAILS
            message = self._format_email_message(
                ERR_MESSAGE, emails, package, version, self.errors
            )
            self.logger.info("Cloud Policy: Sending error email for {}, to {}".format(package, emails))
            self._send_email(emails, message)

    def _run_azure_tests(self, package, series, pocket):
        """Runs Azure's required package tests.

        :param package The name of the package to test
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param pocket The name of the pocket the package should be installed from (e.g. proposed)
        """
        urn = self.SERIES_TO_URN.get(series, None)
        if urn is None:
            return

        self.logger.info("Cloud Policy: Running Azure tests for: {} in {}-{}".format(package, series, pocket))
        subprocess.run(
            [
                "/snap/bin/cloud-test-framework",
                "--instance-prefix", "britney-{}-{}-{}".format(package, series, pocket),
                "--install-archive-package", "{}/{}".format(package, pocket),
                "azure_gen2",
                "--location", "westeurope",
                "--vm-size", "Standard_D2s_v5",
                "--urn", urn,
                "run-test", "package-install-with-reboot",
            ],
            cwd=self.WORK_DIR
        )

        results_file_paths = self._find_results_files(r"TEST-NetworkTests-[0-9]*.xml")
        self._parse_xunit_test_results("Azure", results_file_paths)

    def _find_results_files(self, file_regex):
        """Find any test results files that match the given regex pattern.

        :param file_regex A regex pattern to use for matching the name of the results file.
        """
        file_paths = []
        for file in os.listdir(self.WORK_DIR):
            if re.fullmatch(file_regex, file):
                file_paths.append(PurePath(self.WORK_DIR, file))

        return file_paths

    def _parse_xunit_test_results(self, cloud, results_file_paths):
        """Parses and stores any failure or error test results.

        :param cloud The name of the cloud, use for storing the results.
        :results_file_paths List of paths to results files
        """
        for file_path in results_file_paths:
            with open(file_path) as file:
                ts, tr = xunitparser.parse(file)

                for testcase, message in tr.failures:
                    self.store_test_result(self.failures, cloud, testcase.methodname, message)

                for testcase, message in tr.errors:
                    self.store_test_result(self.errors, cloud, testcase.methodname, message)

    def store_test_result(self, results, cloud, test_name, message):
        """Adds the test to the results hash under the given cloud.

        Results format:
            {
                cloud1: {
                    test_name1: message1
                    test_name2: message2
                },
                cloud2: ...
            }

        :param results A hash to add results to
        :param cloud The name of the cloud
        :param message The exception or assertion error given by the test
        """
        if cloud not in results:
            results[cloud] = {}

        results[cloud][test_name] = message

    def _format_email_message(self, template, emails, package, version, test_results):
        """Insert given parameters into the email template."""
        series = self.series
        pocket = self.pocket
        results = json.dumps(test_results, indent=4)
        recipients = ", ".join(emails)
        message= template.format(**locals())

        return message

    def _send_email(self, emails, message):
        """Send an email

        :param emails List of emails to send to
        :param message The content of the email
        """
        try:
            server = smtplib.SMTP(self.email_host)
            server.sendmail("noreply+proposed-migration@ubuntu.com", emails, message)
            server.quit()
        except socket.error as err:
            self.logger.error("Cloud Policy: Failed to send mail! Is SMTP server running?")
            self.logger.error(err)

    def _setup_work_directory(self):
        """Create a directory for tests to be run in."""
        self._cleanup_work_directory()

        os.makedirs(self.WORK_DIR)

    def _cleanup_work_directory(self):
        """Delete the the directory used for running tests."""
        if os.path.exists(self.WORK_DIR):
            shutil.rmtree(self.WORK_DIR)

