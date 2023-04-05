import json
import os
import re
import shutil
import smtplib
import socket
import subprocess
import xml.etree.ElementTree as ET

from britney2 import SuiteClass
from britney2.policies.policy import BasePolicy
from britney2.policies import PolicyVerdict

class MissingURNException(Exception):
    pass

FAIL_MESSAGE = """From: Ubuntu Release Team <noreply+proposed-migration@ubuntu.com>
To: {recipients}
X-Proposed-Migration: notice
Subject: [proposed-migration] {package} {version} in {series} failed Cloud tests.

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
Subject: [proposed-migration] {package} {version} in {series} had errors running Cloud Tests.

Hi,

During Cloud tests of {package} {version} the following errors occurred:

{results}

If you have any questions about this email, please ask them in #ubuntu-release channel on libera.chat.

Regards, Ubuntu Release Team.
"""
class CloudPolicy(BasePolicy):
    PACKAGE_SET_FILE = "cloud_package_set.json"
    STATE_FILE = "cloud_state"
    DEFAULT_EMAILS = ["cpc@canonical.com"]
    TEST_LOG_FILE = "CTF.log"

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
        self.state_filename = getattr(self.options, "cloud_state_file", self.STATE_FILE)

        self.reporting_enabled = getattr(self.options, "cloud_enable_reporting", "no")

        self.state = {}

        adt_ppas = getattr(self.options, "adt_ppas", "")
        if not isinstance(adt_ppas, list):
            adt_ppas = adt_ppas.split()
        ppas = self._parse_ppas(adt_ppas)

        if len(ppas) == 0:
            self.sources = ["proposed"]
            self.source_type = "archive"
        else:
            self.sources = ppas
            self.source_type = "ppa"

        self.failures = {}
        self.errors = {}
        self.email_needed = False
        self.package_set = {}

    def initialise(self, britney):
        super().initialise(britney)

        self.package_set = self._retrieve_cloud_package_set_for_series(
            self.PACKAGE_SET_FILE,
            self.options.series
        )
        self._load_state()

    def apply_src_policy_impl(self, policy_info, item, source_data_tdist, source_data_srcdist, excuse):
        self.logger.info("Cloud Policy: Looking at {}".format(item.package))
        clouds_to_test = self._check_if_tests_required(self.package_set, item.package)
        if len(clouds_to_test) == 0:
            verdict = PolicyVerdict.PASS
            excuse.add_verdict_info(
                verdict,
                "Cloud tests not required because package is not in the cloud package set."
            )
            return verdict

        if self.dry_run:
            self.logger.info(
                "Cloud Policy: Dry run would test {} in {}".format(item.package , self.options.series)
            )
            return PolicyVerdict.PASS

        self._setup_work_directory()
        self.failures = {}
        self.errors = {}

        if self.reporting_enabled == "yes":
            self._report_test_start(item.package, source_data_srcdist.version, self.options.series)

        self._run_cloud_tests(clouds_to_test, item.package, source_data_srcdist.version, self.options.series,
                              self.sources, self.source_type)

        if len(self.failures) > 0 or len(self.errors) > 0:
            if self.reporting_enabled == "yes":
                if len(self.failures) > 0:
                    self._report_test_result(item.package, source_data_srcdist.version, self.options.series, "fail")
                else:
                    self._report_test_result(item.package, source_data_srcdist.version, self.options.series, "error")

            if self.email_needed:
                self._send_emails_if_needed(item.package, source_data_srcdist.version, self.options.series)

            self._cleanup_work_directory()
            verdict = PolicyVerdict.REJECTED_PERMANENTLY
            info = self._generate_verdict_info(self.failures, self.errors)
            excuse.add_verdict_info(verdict, info)
            return verdict
        else:
            if self.reporting_enabled == "yes":
                self._report_test_result(item.package, source_data_srcdist.version, self.options.series, "pass")

            self._cleanup_work_directory()
            verdict = PolicyVerdict.PASS
            excuse.add_verdict_info(verdict, "Cloud tests passed.")
            return verdict

    def _mark_tests_run(self, package, version, series, source_type, cloud):
        """Mark the selected package version as already tested.
        This takes which cloud we're testing into consideration.

        :param package The name of the package to test
        :param version Version of the package
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param source_type Either 'archive' or 'ppa'
        :param cloud The name of the cloud being tested (e.g. azure)
        """
        if cloud not in self.state:
            self.state[cloud] = {}
        if source_type not in self.state[cloud]:
            self.state[cloud][source_type] = {}
        if series not in self.state[cloud][source_type]:
            self.state[cloud][source_type][series] = {}
        self.state[cloud][source_type][series][package] = { 
            "version": version,
            "failures": len(self.failures),
            "errors": len(self.errors)
        }
        
        self.email_needed = True

        self._save_state()

    def _check_if_tests_run(self, package, version, series, source_type, cloud):
        """Check if tests were already run for the given package version.
        This takes which cloud we're testing into consideration.

        If failures=0 and errors>0 then tests are considered to have not ran because
        of previous test errors.

        :param package The name of the package to test
        :param version Version of the package
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param source_type Either 'archive' or 'ppa'
        :param cloud The name of the cloud being tested (e.g. azure)
        """
        try:
            package_state = self.state[cloud][source_type][series][package]
            same_version = package_state["version"] == version
            only_errors = package_state["failures"] == 0 and package_state["errors"] > 0

            return same_version and not only_errors
        except KeyError:
            return False

    def _set_previous_failure_and_error(self, package, version, series, source_type, cloud):
        """Sets the failures and errors from the previous run.
        This takes which cloud we're testing into consideration.

        :param package The name of the package to test
        :param version Version of the package
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param source_type Either 'archive' or 'ppa'
        :param cloud The name of the cloud being tested (e.g. azure)
        """
        if self.state[cloud][source_type][series][package]["failures"] > 0:
            self.failures[cloud] = {}

        if self.state[cloud][source_type][series][package]["errors"] > 0:
            self.errors[cloud] = {}

    def _load_state(self):
        """Load the save state of which packages have already been tested."""
        if os.path.exists(self.state_filename):
            with open(self.state_filename, encoding="utf-8") as data:
                self.state = json.load(data)
            self.logger.info("Loaded cloud policy state file %s" % self.state_filename)

    def _save_state(self):
        """Save which packages have already been tested."""
        with open(self.state_filename, "w", encoding="utf-8") as data:
            json.dump(self.state, data)
        self.logger.info("Saved cloud policy state file %s" % self.state_filename)

    def _retrieve_cloud_package_set_for_series(self, file_path, series):
        """Retrieves a set of packages for the given series in which cloud
        tests should be run.

        :param file_path The path to the package set file.
        :param series The Ubuntu codename for the series (e.g. jammy)
        """
        package_set = {}

        with open(file_path) as file:
            full_package_set = json.load(file)
            for cloud, cloud_set in full_package_set.items():
                for cloud_series, series_set in cloud_set.items():
                    if cloud_series == series:
                        package_set[cloud] = series_set

        return package_set

    def _check_if_tests_required(self, package_set, package):
        """Checks the package set to see if the package is present and therefore tests are required.
        Returns a list of the clouds which contain the package.

        :param package_set The cloud package set as a dictionary.
        :param package The name of the source package.
        :returns A list of clouds which require tests for this package.
        """
        clouds_to_test = []
        for cloud, packages in package_set.items():
            if package in packages:
                clouds_to_test.append(cloud)

        return clouds_to_test

    def _run_cloud_tests(self, clouds, package, version, series, sources, source_type):
        """Runs any cloud tests for the given package.
        Nothing is returned but test failures and errors are stored in instance variables.

        :param clouds A list of clouds which need tests run
        :param package The name of the package to test
        :param version Version of the package
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param sources List of sources where the package should be installed from (e.g. [proposed] or PPAs)
        :param source_type Either 'archive' or 'ppa'
        """
        for cloud in clouds:
            if cloud == "azure":
                self._run_azure_tests(package, version, series, sources, source_type)
            else:
                raise RuntimeError("Cloud Policy: Unexpected cloud to test: {}".format(cloud))

    def _send_emails_if_needed(self, package, version, series):
        """Sends email(s) if there are test failures and/or errors

        :param package The name of the package that was tested
        :param version The version number of the package
        :param series The Ubuntu codename for the series (e.g. jammy)
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

    def _run_azure_tests(self, package, version, series, sources, source_type):
        """Runs Azure's required package tests.

        :param package The name of the package to test
        :param version Version of the package
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param sources List of sources where the package should be installed from (e.g. [proposed] or PPAs)
        :param source_type Either 'archive' or 'ppa'
        """
        if self._check_if_tests_run(package, version, series, source_type, "azure"):
            self._set_previous_failure_and_error(package, version, series, source_type, "azure")
            self.logger.info("Cloud Policy: already tested {}".format(package))
            return

        urn = self._retrieve_urn(series)
        binaries = self._retrieve_binaries("azure", package)

        self.logger.info("Cloud Policy: Running Azure tests for: {} in {}".format(package, series))
        params = [
            "/snap/bin/cloud-test-framework",
            "--instance-prefix", "britney-{}-{}".format(package, series)
        ]
        params.extend(self._format_install_flags(binaries, sources, source_type))
        params.extend(
            [
                "azure_gen2",
                "--location", getattr(self.options, "cloud_azure_location", "westeurope"),
                "--vm-size", getattr(self.options, "cloud_azure_vm_size", "Standard_D2s_v5"),
                "--urn", urn,
                "run-test", "package-install-with-reboot",
            ]
        )

        result = None
        try:
            with open(os.path.join(self.work_dir, self.TEST_LOG_FILE), "w") as file:
                result = subprocess.run(
                    params,
                    cwd=self.work_dir,
                    stdout=file,
                    stderr=subprocess.PIPE,
                    universal_newlines=True
                )
                result.check_returncode()
        except subprocess.CalledProcessError:
            self._store_test_result(
                self.errors, "azure", "testing_error", result.stderr
            )
        finally:
            results_file_paths = self._find_results_files(r"TEST-NetworkTests-[0-9]*.xml")
            self._parse_xunit_test_results("Azure", results_file_paths)
            self._store_extra_test_result_info(self, package)
            self._mark_tests_run(package, version, series, source_type, "azure")

    def _retrieve_urn(self, series):
        """Retrieves an URN from the configuration options based on series.
        An URN identifies a unique image in Azure.

        :param series The ubuntu codename for the series (e.g. jammy)
        """
        urn = getattr(self.options, "cloud_azure_{}_urn".format(series), None)

        if urn is None:
            raise MissingURNException("No URN configured for {}".format(series))

        return urn

    def _retrieve_binaries(self, cloud, source_package):
        """Given a source package name and cloud, retrieves the associated binary package names from the package set.

        :param source_package The name of a source package.
        :returns The list of binary package names.
        """
        return self.package_set[cloud][source_package]

    def _find_results_files(self, file_regex):
        """Find any test results files that match the given regex pattern.

        :param file_regex A regex pattern to use for matching the name of the results file.
        """
        file_paths = []
        for file in os.listdir(self.work_dir):
            if re.fullmatch(file_regex, file):
                file_paths.append(os.path.join(self.work_dir, file))

        return file_paths

    def _parse_xunit_test_results(self, cloud, results_file_paths):
        """Parses and stores any failure or error test results.

        :param cloud The name of the cloud, use for storing the results.
        :param results_file_paths List of paths to results files
        """
        for file_path in results_file_paths:
            with open(file_path) as file:
                xml = ET.parse(file)
                root = xml.getroot()

                if root.tag == "testsuites":
                    for testsuite in root:
                        self._parse_xunit_testsuite(cloud, testsuite)
                else:
                    self._parse_xunit_testsuite(cloud, root)

    def _parse_xunit_testsuite(self, cloud, root):
        """Parses the xunit testsuite and stores any failure or error test results.

        :param cloud The name of the cloud, used for storing the results.
        :param root An XML tree root.
        """
        for el in root:
            if el.tag == "testcase":
                for e in el:
                    if e.tag == "failure":
                        type = e.attrib.get('type')
                        message = e.attrib.get('message')
                        info = "{}: {}".format(type, message)
                        self._store_test_result(
                            self.failures, cloud, el.attrib.get('name'), info
                        )
                    if e.tag == "error":
                        type = e.attrib.get('type')
                        message = e.attrib.get('message')
                        info = "{}: {}".format(type, message)
                        self._store_test_result(
                            self.errors, cloud, el.attrib.get('name'), info
                        )

    def _store_test_result(self, results, cloud, test_name, message):
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

    def _store_extra_test_result_info(self, cloud, package):
        """Stores any information beyond the test results and stores it in the results dicts
        under Cloud->extra_info

        Stores any information retrieved under the cloud's section in failures/errors but will
        store nothing if failures/errors are empty.

        :param cloud The name of the cloud
        :param package The name of the package to test
        """
        if len(self.failures) == 0 and len(self.errors) == 0:
            return

        extra_info = {}

        install_source = self._retrieve_package_install_source_from_test_output(package)
        if install_source:
            extra_info["install_source"] = install_source

        if len(self.failures.get(cloud, {})) > 0:
            self._store_test_result(self.failures, cloud, "extra_info", extra_info)

        if len(self.errors.get(cloud, {})) > 0:
            self._store_test_result(self.errors, cloud, "extra_info", extra_info)

    def _retrieve_package_install_source_from_test_output(self, package):
        """Checks the test logs for apt logs which show where the package was installed from.
        Useful if multiple PPA sources are defined since we won't explicitly know the exact source.

        Will return nothing unless exactly one matching line is found.

        :param package The name of the package to test
        """
        possible_locations = []
        with open(os.path.join(self.work_dir, self.TEST_LOG_FILE), "r") as file:
            for line in file:
                if package not in line:
                    continue

                if "Get:" not in line:
                    continue

                if " {} ".format(package) not in line:
                    continue

                possible_locations.append(line)

        if len(possible_locations) == 1:
            return possible_locations[0]
        else:
            return None

    def _format_email_message(self, template, emails, package, version, test_results):
        """Insert given parameters into the email template."""
        series = self.options.series
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

    def _format_install_flags(self, binaries, sources, source_type):
        """Determine the flags required to install the package from the given sources

        :param binaries A list of binary package names
        :param sources List of sources where the package should be installed from (e.g. [proposed] or PPAs)
        :param source_type Either 'archive' or 'ppa'
        """
        install_flags = []

        for source in sources:
            flag = ""
            if source_type == "archive":
                flag = "--install-archive-package"
            elif source_type == "ppa":
                flag = "--install-ppa-package"
            else:
                raise RuntimeError("Cloud Policy: Unexpected source type, {}".format(source_type))

            for binary in binaries:
                install_flags.append(flag)
                install_flags.append("{}/{}".format(binary, source))

        return install_flags

    def _parse_ppas(self, ppas):
        """Parse PPA list to store in format expected by cloud tests

        Only supports PPAs provided with a fingerprint

        Britney private PPA format:
            'user:token@team/name:fingerprint'
        Britney public PPA format:
            'team/name:fingerprint'
        Cloud private PPA format:
            'https://user:token@private-ppa.launchpadcontent.net/team/name/ubuntu=fingerprint
        Cloud public PPA format:
            'https://ppa.launchpadcontent.net/team/name/ubuntu=fingerprint

        :param ppas List of PPAs in Britney approved format
        :return A list of PPAs in valid cloud test format. Can return an empty list if none found.
        """
        cloud_ppas = []

        for ppa in ppas:
            if '@' in ppa:
                match = re.match("^(?P<auth>.+:.+)@(?P<name>.+):(?P<fingerprint>.+$)", ppa)
                if not match:
                    raise RuntimeError('Private PPA %s not following required format (user:token@team/name:fingerprint)', ppa)

                formatted_ppa = "https://{}@private-ppa.launchpadcontent.net/{}/ubuntu={}".format(
                    match.group("auth"), match.group("name"), match.group("fingerprint")
                )
                cloud_ppas.append(formatted_ppa)
            else:
                match = re.match("^(?P<name>.+):(?P<fingerprint>.+$)", ppa)
                if not match:
                    raise RuntimeError('Public PPA %s not following required format (team/name:fingerprint)', ppa)

                formatted_ppa = "https://ppa.launchpadcontent.net/{}/ubuntu={}".format(
                    match.group("name"), match.group("fingerprint")
                )
                cloud_ppas.append(formatted_ppa)

        return cloud_ppas

    def _report_test_start(self, package, version, series):
        """
        Add an entry to the report to show that the package has started tests for this series and version.

        :param package The name of the package to test
        :param version Version of the package
        :param series The Ubuntu codename for the series (e.g. jammy)
        """
        params = [
            "/snap/bin/ctf-britney-tools", "table-commands", "write-data",
            series, package, version
        ]

        subprocess.run(params)

    def _report_test_result(self, package, version, series, result):
        """
        Provides a simple summary of the test result to the report; whether it was a pass, fail or error.

        :param package The name of the package to test
        :param version Version of the package
        :param series The Ubuntu codename for the series (e.g. jammy)
        :param result A string describing the result (pass, fail, error)
        """
        params = [
            "/snap/bin/ctf-britney-tools", "table-commands", "write-test-result",
            series, package, version, result
        ]

        subprocess.run(params)

    def _setup_work_directory(self):
        """Create a directory for tests to be run in."""
        self._cleanup_work_directory()

        os.makedirs(self.work_dir)

    def _cleanup_work_directory(self):
        """Delete the the directory used for running tests."""
        if os.path.exists(self.work_dir):
            shutil.rmtree(self.work_dir)

