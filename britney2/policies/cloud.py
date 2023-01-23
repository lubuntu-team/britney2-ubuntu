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

class MissingURNException(Exception):
    pass

FAIL_MESSAGE = """From: Ubuntu Release Team <noreply+proposed-migration@ubuntu.com>
To: {recipients}
X-Proposed-Migration: notice
Subject: [proposed-migration] {package} {version} in {series}, {source} failed Cloud tests.

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
Subject: [proposed-migration] {package} {version} in {series}, {source} had errors running Cloud Tests.

Hi,

During Cloud tests of {package} {version} the following errors occurred:

{results}

If you have any questions about this email, please ask them in #ubuntu-release channel on libera.chat.

Regards, Ubuntu Release Team.
"""
class CloudPolicy(BasePolicy):
    PACKAGE_SET_FILE = "cloud_package_set"
    DEFAULT_EMAILS = ["cpc@canonical.com"]

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
        self.work_dir = getattr(self.options, "cloud_work_dir", "cloud_tests")
        self.failure_emails = getattr(self.options, "cloud_failure_emails", self.DEFAULT_EMAILS)
        self.error_emails = getattr(self.options, "cloud_error_emails", self.DEFAULT_EMAILS)

        self.source = getattr(self.options, "cloud_source")
        self.source_type = getattr(self.options, "cloud_source_type")

        self.failures = {}
        self.errors = {}

    def initialise(self, britney):
        super().initialise(britney)

        self.package_set = self._retrieve_cloud_package_set_for_series(self.options.series)

    def apply_src_policy_impl(self, policy_info, item, source_data_tdist, source_data_srcdist, excuse):
        if item.package not in self.package_set:
            return PolicyVerdict.PASS

        if self.dry_run:
            self.logger.info(
                "Cloud Policy: Dry run would test {} in {}, {}".format(item.package , self.options.series, self.source)
            )
            return PolicyVerdict.PASS

        self._setup_work_directory()
        self.failures = {}
        self.errors = {}

        self._run_cloud_tests(item.package, self.options.series, self.source, self.source_type)

        if len(self.failures) > 0 or len(self.errors) > 0:
            self._send_emails_if_needed(item.package, source_data_srcdist.version, self.options.series, self.source)

            self._cleanup_work_directory()
            verdict = PolicyVerdict.REJECTED_PERMANENTLY
            info = self._generate_verdict_info(self.failures, self.errors)
            excuse.add_verdict_info(verdict, info)
            return verdict
        else:
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

    def _run_cloud_tests(self, package, series, source, source_type):
        """Runs any cloud tests for the given package.
        Nothing is returned but test failures and errors are stored in instance variables.

        :param package The name of the package to test
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param source Where the package should be installed from (e.g. proposed or a PPA)
        :param source_type Either 'archive' or 'ppa'
        """
        self._run_azure_tests(package, series, source, source_type)

    def _send_emails_if_needed(self, package, version, series, source):
        """Sends email(s) if there are test failures and/or errors

        :param package The name of the package that was tested
        :param version The version number of the package
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param source Where the package should be installed from (e.g. proposed or a PPA)
        """
        if len(self.failures) > 0:
            emails = self.failure_emails
            message = self._format_email_message(
                FAIL_MESSAGE, emails, package, version, self.failures
            )
            self.logger.info("Cloud Policy: Sending failure email for {}, to {}".format(package, emails))
            self._send_email(emails, message)

        if len(self.errors) > 0:
            emails = self.error_emails
            message = self._format_email_message(
                ERR_MESSAGE, emails, package, version, self.errors
            )
            self.logger.info("Cloud Policy: Sending error email for {}, to {}".format(package, emails))
            self._send_email(emails, message)

    def _run_azure_tests(self, package, series, source, source_type):
        """Runs Azure's required package tests.

        :param package The name of the package to test
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param source Where the package should be installed from (e.g. proposed or a PPA)
        :param source_type Either 'archive' or 'ppa'
        """
        urn = self._retrieve_urn(series)
        install_flag = self._determine_install_flag(source_type)

        self.logger.info("Cloud Policy: Running Azure tests for: {} in {}, {}".format(package, series, source))
        subprocess.run(
            [
                "/snap/bin/cloud-test-framework",
                "--instance-prefix", "britney-{}-{}-{}".format(package, series, source),
                install_flag, "{}/{}".format(package, source),
                "azure_gen2",
                "--location", "westeurope",
                "--vm-size", "Standard_D2s_v5",
                "--urn", urn,
                "run-test", "package-install-with-reboot",
            ],
            cwd=self.work_dir
        )

        results_file_paths = self._find_results_files(r"TEST-NetworkTests-[0-9]*.xml")
        self._parse_xunit_test_results("Azure", results_file_paths)

    def _retrieve_urn(self, series):
        """Retrieves an URN from the configuration options based on series.
        An URN identifies a unique image in Azure.

        :param series The ubuntu codename for the series (e.g. jammy)
        """
        urn = getattr(self.options, "cloud_azure_{}_urn".format(series), None)

        if urn is None:
            raise MissingURNException("No URN configured for {}".format(series))

        return urn

    def _find_results_files(self, file_regex):
        """Find any test results files that match the given regex pattern.

        :param file_regex A regex pattern to use for matching the name of the results file.
        """
        file_paths = []
        for file in os.listdir(self.work_dir):
            if re.fullmatch(file_regex, file):
                file_paths.append(PurePath(self.work_dir, file))

        return file_paths

    def _parse_xunit_test_results(self, cloud, results_file_paths):
        """Parses and stores any failure or error test results.

        :param cloud The name of the cloud, use for storing the results.
        :param results_file_paths List of paths to results files
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
        series = self.options.series
        source = self.source
        results = json.dumps(test_results, indent=4)
        recipients = ", ".join(emails)
        message = template.format(**locals())

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

    def _generate_verdict_info(self, failures, errors):
        info = ""

        if len(failures) > 0:
            fail_clouds = ",".join(list(failures.keys()))
            info += "Cloud testing failed for {}.".format(fail_clouds)

        if len(errors) > 0:
            error_clouds = ",".join(list(errors.keys()))
            info += " Cloud testing had errors for {}.".format(error_clouds)

        return info

    def _determine_install_flag(self, source_type):
        if source_type == "archive":
            return "--install-archive-package"
        elif source_type == "ppa":
            return "--install-ppa-package"
        else:
            raise RuntimeError("Cloud Policy: Unexpected source type, {}".format(source_type))

    def _setup_work_directory(self):
        """Create a directory for tests to be run in."""
        self._cleanup_work_directory()

        os.makedirs(self.work_dir)

    def _cleanup_work_directory(self):
        """Delete the the directory used for running tests."""
        if os.path.exists(self.work_dir):
            shutil.rmtree(self.work_dir)

