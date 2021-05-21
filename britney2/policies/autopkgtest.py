# -*- coding: utf-8 -*-

# Copyright (C) 2013 - 2016 Canonical Ltd.
# Authors:
#   Colin Watson <cjwatson@ubuntu.com>
#   Jean-Baptiste Lallement <jean-baptiste.lallement@canonical.com>
#   Martin Pitt <martin.pitt@ubuntu.com>

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

import calendar
import collections
from copy import deepcopy
from datetime import datetime
from enum import Enum
import os
import json
import tarfile
import io
import itertools
import re
import socket
import sys
import time
import urllib.parse
from urllib.error import HTTPError
from urllib.request import urlopen

import apt_pkg

import britney2.hints

from britney2 import SuiteClass
from britney2.policies.policy import BasePolicy, PolicyVerdict
from britney2.utils import iter_except


class Result(Enum):
    FAIL = 1
    PASS = 2
    NEUTRAL = 3
    NONE = 4
    OLD_FAIL = 5
    OLD_PASS = 6
    OLD_NEUTRAL = 7


EXCUSES_LABELS = {
    "PASS": '<span style="background:#87d96c">Pass</span>',
    "OLD_PASS": '<span style="background:#87d96c">Pass</span>',
    "NEUTRAL": '<span style="background:#e5c545">No test results</span>',
    "OLD_NEUTRAL": '<span style="background:#e5c545">No test results</span>',
    "FAIL": '<span style="background:#ff6666">Failed</span>',
    "OLD_FAIL": '<span style="background:#ff6666">Failed</span>',
    "ALWAYSFAIL": '<span style="background:#e5c545">Not a regression</span>',
    "REGRESSION": '<span style="background:#ff6666">Regression</span>',
    "IGNORE-FAIL": '<span style="background:#e5c545">Ignored failure</span>',
    "RUNNING": '<span style="background:#99ddff">Test in progress</span>',
    "RUNNING-REFERENCE": '<span style="background:#ff6666">Reference test in progress, but real test failed already</span>',
    "RUNNING-ALWAYSFAIL": '<span style="background:#99ddff">Test in progress (will not be considered a regression)</span>',
}

REF_TRIG = 'migration-reference/0'

SECPERDAY = 24 * 60 * 60


def srchash(src):
    '''archive hash prefix for source package'''

    if src.startswith('lib'):
        return src[:4]
    else:
        return src[0]


def added_pkgs_compared_to_target_suite(package_ids, target_suite, *, invert=False):
    if invert:
        pkgs_ids_to_ignore = package_ids - set(target_suite.which_of_these_are_in_the_suite(package_ids))
        names_ignored = {p.package_name for p in pkgs_ids_to_ignore}
    else:
        names_ignored = {p.package_name for p in target_suite.which_of_these_are_in_the_suite(package_ids)}
    yield from (p for p in package_ids if p.package_name not in names_ignored)


def all_leaf_results(test_results):
    for trigger in test_results.values():
        for arch in trigger.values():
            yield from arch.values()


def mark_result_as_old(result):
    '''Convert current result into corresponding old result'''

    if result == Result.FAIL:
        result = Result.OLD_FAIL
    elif result == Result.PASS:
        result = Result.OLD_PASS
    elif result == Result.NEUTRAL:
        result = Result.OLD_NEUTRAL
    return result


class AutopkgtestPolicy(BasePolicy):
    """autopkgtest regression policy for source migrations

    Run autopkgtests for the excuse and all of its reverse dependencies, and
    reject the upload if any of those regress.
    """

    def __init__(self, options, suite_info):
        super().__init__('autopkgtest', options, suite_info, {SuiteClass.PRIMARY_SOURCE_SUITE})
        # tests requested in this and previous runs
        # trigger -> src -> [arch]
        self.pending_tests = None
        self.pending_tests_file = os.path.join(self.state_dir, 'autopkgtest-pending.json')
        self.testsuite_triggers = {}
        self.result_in_baseline_cache = collections.defaultdict(dict)

        # results map: trigger -> src -> arch -> [passed, version, run_id, seen]
        # - trigger is "source/version" of an unstable package that triggered
        #   this test run.
        # - "passed" is a bool
        # - "version" is the package version  of "src" of that test
        # - "run_id" is an opaque ID that identifies a particular test run for
        #   a given src/arch.
        # - "seen" is an approximate time stamp of the test run. How this is
        #   deduced depends on the interface used.
        self.test_results = {}
        if self.options.adt_shared_results_cache:
            self.results_cache_file = self.options.adt_shared_results_cache
        else:
            self.results_cache_file = os.path.join(self.state_dir, 'autopkgtest-results.cache')

        try:
            self.options.adt_ppas = self.options.adt_ppas.strip().split()
        except AttributeError:
            self.options.adt_ppas = []

        self.swift_container = 'autopkgtest-' + options.series
        if self.options.adt_ppas:
            self.swift_container += '-' + options.adt_ppas[-1].replace('/', '-')

        # restrict adt_arches to architectures we actually run for
        self.adt_arches = []
        for arch in self.options.adt_arches.split():
            if arch in self.options.architectures:
                self.adt_arches.append(arch)
            else:
                self.logger.info("Ignoring ADT_ARCHES %s as it is not in architectures list", arch)

    def register_hints(self, hint_parser):
        hint_parser.register_hint_type('force-badtest', britney2.hints.split_into_one_hint_per_package)
        hint_parser.register_hint_type('force-skiptest', britney2.hints.split_into_one_hint_per_package)
        hint_parser.register_hint_type('force-reset-test', britney2.hints.split_into_one_hint_per_package)

    def initialise(self, britney):
        super().initialise(britney)
        # We want to use the "current" time stamp in multiple locations
        self._now = round(time.time())
        # compute inverse Testsuite-Triggers: map, unifying all series
        self.logger.info('Building inverse testsuite_triggers map')
        for suite in self.suite_info:
            for src, data in suite.sources.items():
                for trigger in data.testsuite_triggers:
                    self.testsuite_triggers.setdefault(trigger, set()).add(src)
        target_suite_name = self.suite_info.target_suite.name

        os.makedirs(self.state_dir, exist_ok=True)
        self.read_pending_tests()

        if not hasattr(self.options, 'adt_baseline'):
            # Make adt_baseline optional
            setattr(self.options, 'adt_baseline', None)

        if not hasattr(self.options, 'adt_reference_max_age') or \
           not self.options.adt_reference_max_age:
            self.options.adt_reference_max_age = float('inf')
        else:
            self.options.adt_reference_max_age = \
              int(self.options.adt_reference_max_age) * SECPERDAY

        # read the cached results that we collected so far
        if os.path.exists(self.results_cache_file):
            with open(self.results_cache_file) as f:
                test_results = json.load(f)
                self.test_results = self.check_and_upgrade_cache(test_results)
            self.logger.info('Read previous results from %s', self.results_cache_file)
        else:
            self.logger.info('%s does not exist, re-downloading all results from swift', self.results_cache_file)

        # read in the new results
        if self.options.adt_swift_url.startswith('file://'):
            debci_file = self.options.adt_swift_url[7:]
            if os.path.exists(debci_file):
                with open(debci_file) as f:
                    test_results = json.load(f)
                self.logger.info('Read new results from %s', debci_file)
                # With debci, pending tests are determined from the debci file
                self.pending_tests = {}
                for res in test_results['results']:
                    # Blacklisted tests don't get a version
                    if res['version'] is None:
                        res['version'] = 'blacklisted'
                    (test_suite, triggers, src, arch, ver, status, run_id, seen) = ([
                        res['suite'],
                        res['trigger'],
                        res['package'],
                        res['arch'],
                        res['version'],
                        res['status'],
                        str(res['run_id']),
                        round(calendar.timegm(time.strptime(res['updated_at'][0:-5], '%Y-%m-%dT%H:%M:%S')))])
                    if test_suite != target_suite_name:
                        # not requested for this target suite, so ignore
                        continue
                    if triggers is None:
                        # not requested for this policy, so ignore
                        continue
                    for trigger in triggers.split():
                        if status is None:
                            # still running => pending
                            arch_list = self.pending_tests.setdefault(trigger, {}).setdefault(src, [])
                            if arch not in arch_list:
                                self.logger.info('Pending autopkgtest %s on %s to verify %s', src, arch, trigger)
                                arch_list.append(arch)
                                arch_list.sort()
                        elif status == 'tmpfail':
                            # let's see if we still need it
                            continue
                        else:
                            self.logger.debug('Results %s %s %s added', src, trigger, status)
                            self.add_trigger_to_results(trigger, src, ver, arch, run_id, seen, Result[status.upper()])
            else:
                self.logger.info('%s does not exist, no new data will be processed', debci_file)

        # The cache can contain results against versions of packages that
        # are not in any suite anymore. Strip those out, as we don't want
        # to use those results. Additionally, old references may be
        # filtered out.
        if self.options.adt_baseline == 'reference':
            self.filter_old_results()

        # we need sources, binaries, and installability tester, so for now
        # remember the whole britney object
        self.britney = britney

        # Initialize AMQP connection
        self.amqp_channel = None
        self.amqp_file = None
        if self.options.dry_run:
            return

        amqp_url = self.options.adt_amqp

        if amqp_url.startswith('amqp://'):
            import amqplib.client_0_8 as amqp
            # depending on the setup we connect to a AMQP server
            creds = urllib.parse.urlsplit(amqp_url, allow_fragments=False)
            self.amqp_con = amqp.Connection(creds.hostname, userid=creds.username,
                                            password=creds.password)
            self.amqp_channel = self.amqp_con.channel()
            self.logger.info('Connected to AMQP server')
        elif amqp_url.startswith('file://'):
            # or in Debian and in testing mode, adt_amqp will be a file:// URL
            self.amqp_file = amqp_url[7:]
        else:
            raise RuntimeError('Unknown ADT_AMQP schema %s' % amqp_url.split(':', 1)[0])

    def check_and_upgrade_cache(self, test_results):
        for result in all_leaf_results(test_results):
            try:
                result[0] = Result[result[0]]
            except KeyError:
                # Legacy support
                if isinstance(result[0], type(True)):
                    if result[0]:
                        result[0] = Result.PASS
                    else:
                        result[0] = Result.FAIL
                else:
                    raise
            # More legacy support
            try:
                dummy = result[3]
            except IndexError:
                result.append(self._now)
        return test_results

    def filter_old_results(self):
        '''Remove results for old versions and reference runs from the cache.

        For now, only delete reference runs. If we delete regular
        results after a while, packages with lots of triggered tests may
        never have all the results at the same time.
'''

        test_results = self.test_results

        for (trigger, trigger_data) in test_results.items():
            for (src, results) in trigger_data.items():
                for (arch, result) in results.items():
                    if trigger == REF_TRIG and \
                      result[3] < self._now - self.options.adt_reference_max_age:
                        result[0] = mark_result_as_old(result[0])
                    elif not self.test_version_in_any_suite(src, result[1]):
                        result[0] = mark_result_as_old(result[0])

    def test_version_in_any_suite(self, src, version):
        '''Check if the mentioned version of src is found in a suite

        To prevent regressions in the target suite, the result should be
        from a test with the version of the package in either the source
        suite or the target suite. The source suite is also valid,
        because due to versioned test dependencies and Breaks/Conflicts
        relations, regularly the version in the source suite is used
        during testing.
        '''

        versions = set()
        for suite in self.suite_info:
            try:
                srcinfo = suite.sources[src]
            except KeyError:
                continue
            versions.add(srcinfo.version)

        valid_version = False
        for ver in versions:
            if apt_pkg.version_compare(ver, version) == 0:
                valid_version = True
                break

        return valid_version

    def save_pending_json(self):
        # update the pending tests on-disk cache
        self.logger.info('Updating pending requested tests in %s' % self.pending_tests_file)
        with open(self.pending_tests_file + '.new', 'w') as f:
            json.dump(self.pending_tests, f, indent=2)
        os.rename(self.pending_tests_file + '.new', self.pending_tests_file)

    def save_state(self, britney):
        super().save_state(britney)

        # update the results on-disk cache, unless we are using a r/o shared one
        if not self.options.adt_shared_results_cache:
            self.logger.info('Updating results cache')
            test_results = deepcopy(self.test_results)
            for result in all_leaf_results(test_results):
                result[0] = result[0].name
            with open(self.results_cache_file + '.new', 'w') as f:
                json.dump(test_results, f, indent=2)
            os.rename(self.results_cache_file + '.new', self.results_cache_file)

        self.save_pending_json()

    def apply_src_policy_impl(self, tests_info, item, source_data_tdist, source_data_srcdist, excuse):
        # initialize
        verdict = PolicyVerdict.PASS
        elegible_for_bounty = False
        source_name = item.package
        results_info = []

        # skip/delay autopkgtests until new package is built somewhere
        if not source_data_srcdist.binaries:
            self.logger.info('%s hasn''t been built anywhere, skipping autopkgtest policy', excuse.name)
            verdict = PolicyVerdict.REJECTED_TEMPORARILY
            excuse.add_verdict_info(verdict, "nothing built yet, autopkgtest delayed")

        if (self.options.has_arch_all_buildds and 'all' in excuse.missing_builds) or \
           (not self.options.has_arch_all_buildds and self.options.all_buildarch in excuse.missing_builds):
            self.logger.info('%s hasn''t been built for arch:all, skipping autopkgtest policy', source_name)
            verdict = PolicyVerdict.REJECTED_TEMPORARILY
            excuse.add_verdict_info(verdict, "arch:all not built yet, autopkgtest delayed")

        if verdict == PolicyVerdict.PASS:
            self.logger.debug('Checking autopkgtests for %s', source_name)
            trigger = source_name + '/' + source_data_srcdist.version

            # build a (testsrc, testver) → arch → (status, log_url) map; we trigger/check test
            # results per architecture for technical/efficiency reasons, but we
            # want to evaluate and present the results by tested source package
            # first
            pkg_arch_result = collections.defaultdict(dict)
            for arch in self.adt_arches:
                if arch in excuse.missing_builds:
                    verdict = PolicyVerdict.REJECTED_TEMPORARILY
                    self.logger.info('%s hasn''t been built on arch %s, delay autopkgtest there', source_name, arch)
                    excuse.add_verdict_info(verdict, "arch:%s not built yet, autopkgtest delayed there" % arch)
                elif (arch in excuse.unsatisfiable_on_archs and
                      arch not in excuse.policy_info['depends'].get('skip_dep_check', [])):
                    self.logger.info('%s is uninstallable on arch %s, not running autopkgtest there', source_name, arch)
                    excuse.addinfo("uninstallable on arch %s, not running autopkgtest there" % arch)
                else:
                    self.request_tests_for_source(item, arch, source_data_srcdist, pkg_arch_result, excuse)

            # add test result details to Excuse
            cloud_url = self.options.adt_ci_url + "packages/%(h)s/%(s)s/%(r)s/%(a)s"
            for (testsrc, testver) in sorted(pkg_arch_result):
                arch_results = pkg_arch_result[(testsrc, testver)]
                r = {v[0] for v in arch_results.values()}
                if 'REGRESSION' in r:
                    verdict = PolicyVerdict.REJECTED_PERMANENTLY
                elif ('RUNNING' in r or 'RUNNING-REFERENCE' in r) and verdict == PolicyVerdict.PASS:
                    verdict = PolicyVerdict.REJECTED_TEMPORARILY
                # skip version if still running on all arches
                if not r - {'RUNNING', 'RUNNING-ALWAYSFAIL'}:
                    testver = None

                # A source package is elegible for the bounty if it has tests
                # of its own that pass on all tested architectures.
                if testsrc == source_name and r == {'PASS'}:
                    elegible_for_bounty = True

                if testver:
                    testname = '%s/%s' % (testsrc, testver)
                else:
                    testname = testsrc

                html_archmsg = []
                for arch in sorted(arch_results):
                    (status, run_id, log_url) = arch_results[arch]
                    artifact_url = None
                    retry_url = None
                    history_url = None
                    if self.options.adt_ppas:
                        if log_url.endswith('log.gz'):
                            artifact_url = log_url.replace('log.gz', 'artifacts.tar.gz')
                    else:
                        history_url = cloud_url % {
                            'h': srchash(testsrc), 's': testsrc,
                            'r': self.options.series, 'a': arch}
                    if status == 'REGRESSION':
                        if self.options.adt_retry_url_mech == 'run_id':
                            retry_url = self.options.adt_ci_url + 'api/v1/retry/' + run_id
                        else:
                            retry_url = self.options.adt_ci_url + 'request.cgi?' + \
                                    urllib.parse.urlencode([('release', self.options.series),
                                                            ('arch', arch),
                                                            ('package', testsrc),
                                                            ('trigger', trigger)] +
                                                           [('ppa', p) for p in self.options.adt_ppas])

                    tests_info.setdefault(testname, {})[arch] = \
                        [status, log_url, history_url, artifact_url, retry_url]

                    # render HTML snippet for testsrc entry for current arch
                    if history_url:
                        message = '<a href="%s">%s</a>' % (history_url, arch)
                    else:
                        message = arch
                    message += ': <a href="%s">%s</a>' % (log_url, EXCUSES_LABELS[status])
                    if retry_url:
                        message += ' <a href="%s" style="text-decoration: none;">♻ </a> ' % retry_url
                    if artifact_url:
                        message += ' <a href="%s">[artifacts]</a>' % artifact_url
                    html_archmsg.append(message)

                # render HTML line for testsrc entry, but only when action is
                # or may be required
                if r - {'PASS', 'NEUTRAL', 'RUNNING-ALWAYSFAIL', 'ALWAYSFAIL', 'IGNORE-FAIL'}:
                    results_info.append("autopkgtest for %s: %s" % (testname, ', '.join(html_archmsg)))

        if verdict != PolicyVerdict.PASS:
            # check for force-skiptest hint
            hints = self.hints.search('force-skiptest', package=source_name, version=source_data_srcdist.version)
            if hints:
                excuse.addreason('skiptest')
                excuse.addinfo("Should wait for tests relating to %s %s, but forced by %s" %
                               (source_name, source_data_srcdist.version, hints[0].user))
                verdict = PolicyVerdict.PASS_HINTED
            else:
                excuse.addreason('autopkgtest')

        if self.options.adt_success_bounty and verdict == PolicyVerdict.PASS and elegible_for_bounty:
            excuse.add_bounty('autopkgtest', int(self.options.adt_success_bounty))
        if self.options.adt_regression_penalty and \
           verdict in {PolicyVerdict.REJECTED_PERMANENTLY, PolicyVerdict.REJECTED_TEMPORARILY}:
            excuse.add_penalty('autopkgtest', int(self.options.adt_regression_penalty))
            # In case we give penalties instead of blocking, we must always pass
            verdict = PolicyVerdict.PASS
        for i in results_info:
            if verdict.is_rejected:
                excuse.add_verdict_info(verdict, i)
            else:
                excuse.addinfo(i)

        return verdict

    #
    # helper functions
    #

    @classmethod
    def has_autodep8(kls, srcinfo, binaries):
        '''Check if package  is covered by autodep8

        srcinfo is an item from self.britney.sources
        binaries is self.britney.binaries['unstable'][arch]
        '''
        # autodep8?
        for t in srcinfo.testsuite:
            if t.startswith('autopkgtest-pkg'):
                return True

        # DKMS: some binary depends on "dkms"
        for pkg_id in srcinfo.binaries:
            try:
                bininfo = binaries[pkg_id.package_name]
            except KeyError:
                continue
            if 'dkms' in (bininfo.depends or ''):
                return True
        return False

    def request_tests_for_source(self, item, arch, source_data_srcdist, pkg_arch_result, excuse):
        pkg_universe = self.britney.pkg_universe
        target_suite = self.suite_info.target_suite
        source_suite = item.suite
        sources_s = item.suite.sources
        packages_s_a = item.suite.binaries[arch]
        source_name = item.package
        source_version = source_data_srcdist.version
        # request tests (unless they were already requested earlier or have a result)
        tests = self.tests_for_source(source_name, source_version, arch, excuse)
        is_huge = False
        try:
            is_huge = len(tests) > int(self.options.adt_huge)
        except AttributeError:
            pass

        # Here we figure out what is required from the source suite
        # for the test to install successfully.
        #
        # Loop over all binary packages from trigger and
        # recursively look up which *versioned* dependencies are
        # only satisfied in the source suite.
        #
        # For all binaries found, look up which packages they
        # break/conflict with in the target suite, but not in the
        # source suite. The main reason to do this is to cover test
        # dependencies, so we will check Testsuite-Triggers as
        # well.
        #
        # OI: do we need to do the first check in a smart way
        # (i.e. only for the packages that are actully going to be
        # installed) for the breaks/conflicts set as well, i.e. do
        # we need to check if any of the packages that we now
        # enforce being from the source suite, actually have new
        # versioned depends and new breaks/conflicts.
        #
        # For all binaries found, add the set of unique source
        # packages to the list of triggers.

        bin_triggers = set()
        bin_new = set(source_data_srcdist.binaries)
        for binary in iter_except(bin_new.pop, KeyError):
            if binary in bin_triggers:
                continue
            bin_triggers.add(binary)

            # Check if there is a dependency that is not
            # available in the target suite.
            # We add slightly too much here, because new binaries
            # will also show up, but they are already properly
            # installed. Nevermind.
            depends = pkg_universe.dependencies_of(binary)
            # depends is a frozenset{frozenset{BinaryPackageId, ..}}
            for deps_of_bin in depends:
                if target_suite.any_of_these_are_in_the_suite(deps_of_bin):
                    # if any of the alternative dependencies is already
                    # satisfied in the target suite, we can just ignore it
                    continue
                # We'll figure out which version later
                bin_new.update(added_pkgs_compared_to_target_suite(deps_of_bin, target_suite))

        # Check if the package breaks/conflicts anything. We might
        # be adding slightly too many source packages due to the
        # check here as a binary package that is broken may be
        # coming from a different source package in the source
        # suite. Nevermind.
        bin_broken = set()
        for binary in bin_triggers:
            # broken is a frozenset{BinaryPackageId, ..}
            broken = pkg_universe.negative_dependencies_of(binary)
            broken_in_target = {p.package_name for p in target_suite.which_of_these_are_in_the_suite(broken)}
            broken_in_source = {p.package_name for p in source_suite.which_of_these_are_in_the_suite(broken)}
            # We want packages with a newer version in the source suite that
            # no longer has the conflict. This is an approximation
            broken_filtered = set(
                p for p in broken if
                p.package_name in broken_in_target and
                p.package_name not in broken_in_source)
            # We add the version in the target suite, but the code below will
            # change it to the version in the source suite
            bin_broken.update(broken_filtered)
        bin_triggers.update(bin_broken)

        triggers = set()
        for binary in bin_triggers:
            if binary.architecture == arch:
                try:
                    source_of_bin = packages_s_a[binary.package_name].source
                    triggers.add(
                        source_of_bin + '/' +
                        sources_s[source_of_bin].version)
                except KeyError:
                    # Apparently the package was removed from
                    # unstable e.g. if packages are replaced
                    # (e.g. -dbg to -dbgsym)
                    pass
                if binary not in source_data_srcdist.binaries:
                    for tdep_src in self.testsuite_triggers.get(binary.package_name, set()):
                        try:
                            triggers.add(
                                tdep_src + '/' +
                                sources_s[tdep_src].version)
                        except KeyError:
                            # Apparently the source was removed from
                            # unstable (testsuite_triggers are unified
                            # over all suites)
                            pass
        trigger = source_name + '/' + source_version
        triggers.discard(trigger)
        triggers_list = sorted(list(triggers))
        triggers_list.insert(0, trigger)

        for (testsrc, testver) in tests:
            self.pkg_test_request(testsrc, arch, triggers_list, huge=is_huge)
            (result, real_ver, run_id, url) = self.pkg_test_result(testsrc, testver, arch, trigger)
            pkg_arch_result[(testsrc, real_ver)][arch] = (result, run_id, url)

    def tests_for_source(self, src, ver, arch, excuse):
        '''Iterate over all tests that should be run for given source and arch'''

        source_suite = self.suite_info.primary_source_suite
        target_suite = self.suite_info.target_suite
        sources_info = target_suite.sources
        binaries_info = target_suite.binaries[arch]

        reported_pkgs = set()

        tests = []

        # gcc-N triggers tons of tests via libgcc1, but this is mostly in vain:
        # gcc already tests itself during build, and it is being used from
        # -proposed, so holding it back on a dozen unrelated test failures
        # serves no purpose. Just check some key packages which actually use
        # gcc during the test, and doxygen as an example for a libgcc user.
        if src.startswith('gcc-'):
            if re.match(r'gcc-\d+$', src) or src == 'gcc-defaults':
                # add gcc's own tests, if it has any
                srcinfo = source_suite.sources[src]
                if 'autopkgtest' in srcinfo.testsuite:
                    tests.append((src, ver))
                for test in ['binutils', 'fglrx-installer', 'doxygen', 'linux']:
                    try:
                        tests.append((test, sources_info[test].version))
                    except KeyError:
                        # no package in that series? *shrug*, then not (mostly for testing)
                        pass
                return tests
            else:
                # for other compilers such as gcc-snapshot etc. we don't need
                # to trigger anything
                return []

        # Debian doesn't have linux-meta, but Ubuntu does
        # for linux themselves we don't want to trigger tests -- these should
        # all come from linux-meta*. A new kernel ABI without a corresponding
        # -meta won't be installed and thus we can't sensibly run tests against
        # it.
        if src.startswith('linux'):
            if src.startswith('linux-signed'):
                meta = src.replace('linux-signed', 'linux-meta')
            else:
                meta = src.replace('linux', 'linux-meta')
            if meta in sources_info or meta in source_suite.sources:
                return []

        # we want to test the package itself, if it still has a test in unstable
        # but only if the package actually exists on this arch
        srcinfo = source_suite.sources[src]
        if ('autopkgtest' in srcinfo.testsuite or self.has_autodep8(srcinfo, binaries_info)) and \
           len(excuse.packages[arch]) > 0:
            reported_pkgs.add(src)
            tests.append((src, ver))

        extra_bins = []
        # Debian doesn't have linux-meta, but Ubuntu does
        # Hack: For new kernels trigger all DKMS packages by pretending that
        # linux-meta* builds a "dkms" binary as well. With that we ensure that we
        # don't regress DKMS drivers with new kernel versions.
        if src.startswith('linux-meta'):
            # does this have any image on this arch?
            for pkg_id in srcinfo.binaries:
                if pkg_id.architecture == arch and '-image' in pkg_id.package_name:
                    try:
                        extra_bins.append(binaries_info['dkms'].pkg_id)
                    except KeyError:
                        pass

        pkg_universe = self.britney.pkg_universe
        # plus all direct reverse dependencies and test triggers of its
        # binaries which have an autopkgtest
        for binary in itertools.chain(srcinfo.binaries, extra_bins):
            rdeps = pkg_universe.reverse_dependencies_of(binary)
            for rdep in rdeps:
                try:
                    rdep_src = binaries_info[rdep.package_name].source
                    # Don't re-trigger the package itself here; this should
                    # have been done above if the package still continues to
                    # have an autopkgtest in unstable.
                    if rdep_src == src:
                        continue
                except KeyError:
                    continue

                rdep_src_info = sources_info[rdep_src]
                if 'autopkgtest' in rdep_src_info.testsuite or self.has_autodep8(rdep_src_info, binaries_info):
                    if rdep_src not in reported_pkgs:
                        tests.append((rdep_src, rdep_src_info.version))
                        reported_pkgs.add(rdep_src)

            for tdep_src in self.testsuite_triggers.get(binary.package_name, set()):
                if tdep_src not in reported_pkgs:
                    try:
                        tdep_src_info = sources_info[tdep_src]
                    except KeyError:
                        continue
                    if 'autopkgtest' in tdep_src_info.testsuite or self.has_autodep8(tdep_src_info, binaries_info):
                        for pkg_id in tdep_src_info.binaries:
                            if pkg_id.architecture == arch:
                                tests.append((tdep_src, tdep_src_info.version))
                                reported_pkgs.add(tdep_src)
                                break

        # Hardcode linux-meta →  linux, lxc, glibc, systemd triggers until we get a more flexible
        # implementation: https://bugs.debian.org/779559
        if src.startswith('linux-meta'):
            for pkg in ['lxc', 'lxd', 'glibc', src.replace('linux-meta', 'linux'), 'systemd', 'snapd']:
                if pkg not in reported_pkgs:
                    # does this have any image on this arch?
                    for pkg_id in srcinfo.binaries:
                        if pkg_id.architecture == arch and '-image' in pkg_id.package_name:
                            try:
                                tests.append((pkg, source_suite.sources[pkg].version))
                            except KeyError:
                                try:
                                    tests.append((pkg, sources_info[pkg].version))
                                except KeyError:
                                    # package not in that series? *shrug*, then not
                                    pass
                            break

        tests.sort(key=lambda s_v: s_v[0])
        return tests

    def read_pending_tests(self):
        '''Read pending test requests from previous britney runs

        Initialize self.pending_tests with that data.
        '''
        assert self.pending_tests is None, 'already initialized'
        if not os.path.exists(self.pending_tests_file):
            self.logger.info('No %s, starting with no pending tests', self.pending_tests_file)
            self.pending_tests = {}
            return
        with open(self.pending_tests_file) as f:
            self.pending_tests = json.load(f)
        self.logger.info('Read pending requested tests from %s: %s', self.pending_tests_file, self.pending_tests)

    def latest_run_for_package(self, src, arch):
        '''Return latest run ID for src on arch'''

        # this requires iterating over all triggers and thus is expensive;
        # cache the results
        try:
            return self.latest_run_for_package._cache[src][arch]
        except KeyError:
            pass

        latest_run_id = ''
        for srcmap in self.test_results.values():
            try:
                run_id = srcmap[src][arch][2]
            except KeyError:
                continue
            if run_id > latest_run_id:
                latest_run_id = run_id
        self.latest_run_for_package._cache[arch] = latest_run_id
        return latest_run_id

    latest_run_for_package._cache = collections.defaultdict(dict)

    def download_retry(self, url):
        for retry in range(5):
            try:
                req = urlopen(url, timeout=30)
                code = req.getcode()
                if 200 <= code < 300:
                    return req
            except socket.timeout as e:
                self.logger.info(
                    "Timeout downloading '%s', will retry %d more times."
                    % (url, 5 - retry - 1)
                )
                exc = e
            except HTTPError as e:
                if e.code not in (503, 502):
                    raise
                self.logger.info(
                    "Caught error %d downloading '%s', will retry %d more times."
                    % (e.code, url, 5 - retry - 1)
                )
                exc = e
        else:
            raise exc

    def fetch_swift_results(self, swift_url, src, arch):
        '''Download new results for source package/arch from swift'''

        # Download results for one particular src/arch at most once in every
        # run, as this is expensive
        done_entry = src + '/' + arch
        if done_entry in self.fetch_swift_results._done:
            return
        self.fetch_swift_results._done.add(done_entry)

        # prepare query: get all runs with a timestamp later than the latest
        # run_id for this package/arch; '@' is at the end of each run id, to
        # mark the end of a test run directory path
        # example: <autopkgtest-wily>wily/amd64/libp/libpng/20150630_054517@/result.tar
        query = {'delimiter': '@',
                 'prefix': '%s/%s/%s/%s/' % (self.options.series, arch, srchash(src), src)}

        # determine latest run_id from results
        if not self.options.adt_shared_results_cache:
            latest_run_id = self.latest_run_for_package(src, arch)
            if latest_run_id:
                query['marker'] = query['prefix'] + latest_run_id

        # request new results from swift
        url = os.path.join(swift_url, self.swift_container)
        url += '?' + urllib.parse.urlencode(query)
        f = None
        try:
            f = self.download_retry(url)
            if f.getcode() == 200:
                result_paths = f.read().decode().strip().splitlines()
            elif f.getcode() == 204:  # No content
                result_paths = []
            else:
                # we should not ever end up here as we expect a HTTPError in
                # other cases; e. g. 3XX is something that tells us to adjust
                # our URLS, so fail hard on those
                raise NotImplementedError('fetch_swift_results(%s): cannot handle HTTP code %i' %
                                          (url, f.getcode()))
        except IOError as e:
            # 401 "Unauthorized" is swift's way of saying "container does not exist"
            if hasattr(e, 'code') and e.code == 401:
                self.logger.info('fetch_swift_results: %s does not exist yet or is inaccessible', url)
                return
            # Other status codes are usually a transient
            # network/infrastructure failure. Ignoring this can lead to
            # re-requesting tests which we already have results for, so
            # fail hard on this and let the next run retry.
            self.logger.error('Failure to fetch swift results from %s: %s', url, str(e))
            sys.exit(1)
        finally:
            if f is not None:
                f.close()

        for p in result_paths:
            self.fetch_one_result(
                os.path.join(swift_url, self.swift_container, p, 'result.tar'), src, arch)

    fetch_swift_results._done = set()

    def fetch_one_result(self, url, src, arch):
        '''Download one result URL for source/arch

        Remove matching pending_tests entries.
        '''
        f = None
        try:
            f = self.download_retry(url)
            if f.getcode() == 200:
                tar_bytes = io.BytesIO(f.read())
            else:
                raise NotImplementedError('fetch_one_result(%s): cannot handle HTTP code %i' %
                                          (url, f.getcode()))
        except IOError as e:
            self.logger.error('Failure to fetch %s: %s', url, str(e))
            # we tolerate "not found" (something went wrong on uploading the
            # result), but other things indicate infrastructure problems
            if hasattr(e, 'code') and e.code == 404:
                return
            sys.exit(1)
        finally:
            if f is not None:
                f.close()
        try:
            with tarfile.open(None, 'r', tar_bytes) as tar:
                exitcode = int(tar.extractfile('exitcode').read().strip())
                try:
                    srcver = tar.extractfile('testpkg-version').read().decode().strip()
                except KeyError as e:
                    # We have some buggy results in Ubuntu's swift that break a
                    # full reimport. Sometimes we fake up the exit code (when
                    # we want to convert tmpfails to permanent fails), but an
                    # early bug meant we sometimes didn't include a
                    # testpkg-version.
                    if exitcode in (4, 12, 20):
                        # repair it
                        srcver = "%s unknown" % (src)
                    else:
                        raise
                (ressrc, ver) = srcver.split()
                testinfo = json.loads(tar.extractfile('testinfo.json').read().decode())
        except (KeyError, ValueError, tarfile.TarError) as e:
            self.logger.error('%s is damaged, ignoring: %s', url, str(e))
            # ignore this; this will leave an orphaned request in autopkgtest-pending.json
            # and thus require manual retries after fixing the tmpfail, but we
            # can't just blindly attribute it to some pending test.
            return

        if src != ressrc:
            self.logger.error('%s is a result for package %s, but expected package %s', url, ressrc, src)
            return

        # parse recorded triggers in test result
        for e in testinfo.get('custom_environment', []):
            if e.startswith('ADT_TEST_TRIGGERS='):
                result_triggers = [i for i in e.split('=', 1)[1].split() if '/' in i]
                break
        else:
            self.logger.error('%s result has no ADT_TEST_TRIGGERS, ignoring')
            return

        run_id = os.path.basename(os.path.dirname(url))
        # 20200101_000000 is 15 chars long
        seen = round(calendar.timegm(time.strptime(run_id[0:15], '%Y%m%d_%H%M%S')))
        # allow some skipped tests, but nothing else
        if exitcode in [0, 2]:
            result = Result.PASS
        elif exitcode == 8:
            result = Result.NEUTRAL
        else:
            result = Result.FAIL

        self.logger.info(
            'Fetched test result for %s/%s/%s %s (triggers: %s): %s',
            src, ver, arch, run_id, result_triggers, result.name.lower())

        # remove matching test requests
        for trigger in result_triggers:
            self.remove_from_pending(trigger, src, arch)

        # add this result
        for trigger in result_triggers:
            self.add_trigger_to_results(trigger, src, ver, arch, run_id, seen, result)

    def remove_from_pending(self, trigger, src, arch):
        try:
            arch_list = self.pending_tests[trigger][src]
            arch_list.remove(arch)
            if not arch_list:
                del self.pending_tests[trigger][src]
            if not self.pending_tests[trigger]:
                del self.pending_tests[trigger]
            self.logger.info('-> matches pending request %s/%s for trigger %s', src, arch, trigger)
        except (KeyError, ValueError):
            self.logger.info('-> does not match any pending request for %s/%s', src, arch)

    def add_trigger_to_results(self, trigger, src, ver, arch, run_id, seen, status):
        # Ensure that we got a new enough version
        try:
            (trigsrc, trigver) = trigger.split('/', 1)
        except ValueError:
            self.logger.info('Ignoring invalid test trigger %s', trigger)
            return
        if trigsrc == src and apt_pkg.version_compare(ver, trigver) < 0:
            self.logger.debug('test trigger %s, but run for older version %s, ignoring', trigger, ver)
            return

        result = self.test_results.setdefault(trigger, {}).setdefault(
            src, {}).setdefault(arch, [Result.FAIL, None, '', 0])

        # don't clobber existing passed results with non-passing ones from
        # re-runs, except for reference updates
        if status == Result.PASS or result[0] != Result.PASS or \
           (self.options.adt_baseline == 'reference' and trigger == REF_TRIG):
            result[0] = status
            result[1] = ver
            result[2] = run_id
            result[3] = seen

    def send_test_request(self, src, arch, triggers, huge=False):
        '''Send out AMQP request for testing src/arch for triggers

        If huge is true, then the request will be put into the -huge instead of
        normal queue.
        '''
        if self.options.dry_run:
            return

        params = {'triggers': triggers}
        if self.options.adt_ppas:
            params['ppas'] = self.options.adt_ppas
            qname = 'debci-ppa-%s-%s' % (self.options.series, arch)
        elif huge:
            qname = 'debci-huge-%s-%s' % (self.options.series, arch)
        else:
            qname = 'debci-%s-%s' % (self.options.series, arch)
        params['submit-time'] = datetime.strftime(datetime.utcnow(), '%Y-%m-%d %H:%M:%S%z')

        if self.amqp_channel:
            import amqplib.client_0_8 as amqp
            params = json.dumps(params)
            self.amqp_channel.basic_publish(amqp.Message(src + '\n' + params,
                                                         delivery_mode=2),  # persistent
                                            routing_key=qname)
        else:
            # for file-based submission, triggers are space separated
            params['triggers'] = [' '.join(params['triggers'])]
            params = json.dumps(params)
            assert self.amqp_file
            with open(self.amqp_file, 'a') as f:
                f.write('%s:%s %s\n' % (qname, src, params))

    def pkg_test_request(self, src, arch, full_triggers, huge=False):
        '''Request one package test for one particular trigger

        trigger is "pkgname/version" of the package that triggers the testing
        of src. If huge is true, then the request will be put into the -huge
        instead of normal queue.

        This will only be done if that test wasn't already requested in
        a previous run (i. e. if it's not already in self.pending_tests)
        or if there is already a fresh or a positive result for it. This
        ensures to download current results for this package before
        requesting any test.
'''
        trigger = full_triggers[0]
        uses_swift = not self.options.adt_swift_url.startswith('file://')
        try:
            result = self.test_results[trigger][src][arch]
            has_result = True
        except KeyError:
            has_result = False

        if has_result:
            result_state = result[0]
            version = result[1]
            baseline = self.result_in_baseline(src, arch)
            if result_state in {Result.OLD_PASS, Result.OLD_FAIL, Result.OLD_NEUTRAL}:
                pass
            elif result_state == Result.FAIL and \
                    baseline[0] in {Result.PASS, Result.NEUTRAL, Result.OLD_PASS, Result.OLD_NEUTRAL} and \
                    self.options.adt_retry_older_than and \
                    result[3] + int(self.options.adt_retry_older_than) * SECPERDAY < self._now:
                # We might want to retry this failure, so continue
                pass
            elif not uses_swift:
                # We're done if we don't retrigger and we're not using swift
                return
            elif result_state in {Result.PASS, Result.NEUTRAL}:
                self.logger.debug('%s/%s triggered by %s already known', src, arch, trigger)
                return

        # Without swift we don't expect new results
        if uses_swift:
            self.logger.info('Checking for new results for failed %s/%s for trigger %s', src, arch, trigger)
            self.fetch_swift_results(self.options.adt_swift_url, src, arch)
            # do we have one now?
            try:
                self.test_results[trigger][src][arch]
                return
            except KeyError:
                pass

        self.request_test_if_not_queued(src, arch, trigger, full_triggers, huge=huge)

    def request_test_if_not_queued(self, src, arch, trigger, full_triggers=[], huge=False):
        if not full_triggers:
            full_triggers = [trigger]

        # Don't re-request if it's already pending
        arch_list = self.pending_tests.setdefault(trigger, {}).setdefault(src, [])
        if arch in arch_list:
            self.logger.info('Test %s/%s for %s is already pending, not queueing', src, arch, trigger)
        else:
            self.logger.info('Requesting %s autopkgtest on %s to verify %s', src, arch, trigger)
            arch_list.append(arch)
            arch_list.sort()
            self.send_test_request(src, arch, full_triggers, huge=huge)
            # save pending.json right away, so that we don't re-request if britney crashes
            self.save_pending_json()

    def result_in_baseline(self, src, arch):
        '''Get the result for src on arch in the baseline

        The baseline is optionally all data or a reference set)
        '''

        # this requires iterating over all cached results and thus is expensive;
        # cache the results
        try:
            return self.result_in_baseline_cache[src][arch]
        except KeyError:
            pass

        result_reference = [Result.NONE, None, '', 0]
        if self.options.adt_baseline == 'reference':
            try:
                result_reference = self.test_results[REF_TRIG][src][arch]
                self.logger.debug('Found result for src %s in reference: %s',
                                  src, result_reference[0].name)
            except KeyError:
                self.logger.debug('Found NO result for src %s in reference: %s',
                                  src, result_reference[0].name)
                pass
            self.result_in_baseline_cache[src][arch] = deepcopy(result_reference)
            return result_reference

        result_ever = [Result.FAIL, None, '', 0]
        for srcmap in self.test_results.values():
            try:
                if srcmap[src][arch][0] != Result.FAIL:
                    result_ever = srcmap[src][arch]
                # If we are not looking at a reference run, We don't really
                # care about anything except the status, so we're done
                # once we find a PASS.
                if result_ever[0] == Result.PASS:
                    break
            except KeyError:
                pass

        self.result_in_baseline_cache[src][arch] = deepcopy(result_ever)
        self.logger.debug('Result for src %s ever: %s', src, result_ever[0].name)
        return result_ever

    def pkg_test_result(self, src, ver, arch, trigger):
        '''Get current test status of a particular package

        Return (status, real_version, run_id, log_url) tuple; status is a key in
        EXCUSES_LABELS. run_id is None if the test is still running.
        '''
        target_suite = self.suite_info.target_suite
        binaries_info = target_suite.binaries[arch]

        # determine current test result status
        baseline_result = self.result_in_baseline(src, arch)[0]

        # determine current test result status
        until = self.find_max_lower_force_reset_test(src, ver, arch)

        # Special-case triggers from linux-meta*: we cannot compare results
        # against different kernels, as e. g. a DKMS module might work against
        # the default kernel but fail against a different flavor; so for those,
        # filter the considered results to only those against our kernel
        if trigger.startswith('linux-meta'):
            only_trigger = trigger.split('/', 1)[0]
            self.logger.info('This is a kernel; we will only look for results triggered by %s when considering regressions',
                             trigger)
        else:
            only_trigger = None
        ever_passed = self.check_ever_passed_before(src, ver, arch, until, only_trigger=only_trigger)

        fail_result = 'REGRESSION' if ever_passed else 'ALWAYSFAIL'

        url = None
        run_id = None
        try:
            r = self.test_results[trigger][src][arch]
            ver = r[1]
            run_id = r[2]

            if r[0] in {Result.FAIL, Result.OLD_FAIL}:
                if baseline_result == Result.FAIL:
                    result = 'ALWAYSFAIL'
                elif baseline_result in {Result.NONE, Result.OLD_FAIL}:
                    # Check if the autopkgtest exists in the target suite and request it
                    test_in_target = False
                    try:
                        srcinfo = self.suite_info.target_suite.sources[src]
                        if 'autopkgtest' in srcinfo.testsuite or self.has_autodep8(srcinfo, binaries_info):
                            test_in_target = True
                    except KeyError:
                        pass
                    if test_in_target:
                        # If we want reference tests, request here
                        if self.options.adt_baseline == 'reference':
                            self.request_test_if_not_queued(src, arch, REF_TRIG)
                            if baseline_result == Result.NONE:
                                result = 'RUNNING-REFERENCE'
                            else:
                                result = 'ALWAYSFAIL'
                        else:
                            result = 'ALWAYSFAIL'
                    else:
                        result = fail_result
                else:
                    result = fail_result

                if result == 'REGRESSION' and \
                   self.has_higher_force_reset_test(src, ver, arch):
                    result = 'ALWAYSFAIL'

                if self.has_force_badtest(src, ver, arch):
                    result = 'IGNORE-FAIL'
            else:
                result = r[0].name

            if self.options.adt_swift_url.startswith('file://'):
                url = os.path.join(self.options.adt_ci_url,
                                   'data',
                                   'autopkgtest',
                                   self.options.series,
                                   arch,
                                   srchash(src),
                                   src,
                                   run_id,
                                   'log.gz')
            else:
                url = os.path.join(self.options.adt_swift_url,
                                   self.swift_container,
                                   self.options.series,
                                   arch,
                                   srchash(src),
                                   src,
                                   run_id,
                                   'log.gz')
        except KeyError:
            # no result for src/arch; still running?
            if arch in self.pending_tests.get(trigger, {}).get(src, []):
                if baseline_result != Result.FAIL and not self.has_force_badtest(src, ver, arch):
                    result = 'RUNNING'
                else:
                    result = 'RUNNING-ALWAYSFAIL'
                if self.options.adt_swift_url.startswith('file://'):
                    url = self.options.adt_ci_url + 'status/pending'
                else:
                    url = self.options.adt_ci_url + 'running'
            else:
                raise RuntimeError('Result for %s/%s/%s (triggered by %s) is neither known nor pending!' %
                                   (src, ver, arch, trigger))

        return (result, ver, run_id, url)

    def check_ever_passed_before(self, src, max_ver, arch, min_ver=None, only_trigger=None):
        '''Check if tests for src ever passed on arch for specified range

        If min_ver is specified, it checks that all versions in
        [min_ver, max_ver) have passed; otherwise it checks that
        [min_ver, inf) have passed.'''

        # FIXME: add caching
        for (trigger, srcmap) in self.test_results.items():
            if only_trigger:
                trig = trigger.split('/', 1)[0]
                if only_trigger != trig:
                    continue
            try:
                too_high = apt_pkg.version_compare(srcmap[src][arch][1], max_ver) > 0
                too_low = apt_pkg.version_compare(srcmap[src][arch][1], min_ver) <= 0 if min_ver else False

                if too_high or too_low:
                    continue

                if srcmap[src][arch][0] in (Result.PASS, Result.OLD_PASS):
                    return True
            except KeyError:
                pass
        return False

    def find_max_lower_force_reset_test(self, src, ver, arch):
        '''Find the maximum force-reset-test hint before/including ver'''
        hints = self.hints.search('force-reset-test', package=src)
        found_ver = None

        if hints:
            for hint in hints:
                for mi in hint.packages:
                    if (mi.architecture in ['source', arch] and
                            mi.version != 'all' and
                            apt_pkg.version_compare(mi.version, ver) <= 0 and
                            (found_ver is None or apt_pkg.version_compare(found_ver, mi.version) < 0)):
                        found_ver = mi.version

        return found_ver

    def has_higher_force_reset_test(self, src, ver, arch):
        '''Find if there is a minimum force-reset-test hint after/including ver'''
        hints = self.hints.search('force-reset-test', package=src)

        if hints:
            self.logger.info('Checking hints for %s/%s/%s: %s' % (src, ver, arch, [str(h) for h in hints]))
            for hint in hints:
                for mi in hint.packages:
                    if (mi.architecture in ['source', arch] and
                            mi.version != 'all' and
                            apt_pkg.version_compare(mi.version, ver) >= 0):
                        return True

        return False

    def has_force_badtest(self, src, ver, arch):
        '''Check if src/ver/arch has a force-badtest hint'''

        hints = self.hints.search('force-badtest', package=src)
        if hints:
            self.logger.info('Checking hints for %s/%s/%s: %s', src, ver, arch, [str(h) for h in hints])
            for hint in hints:
                if [mi for mi in hint.packages if mi.architecture in ['source', arch] and
                        (mi.version == 'all' or
                         (mi.version == 'blacklisted' and ver == 'blacklisted') or
                         (mi.version != 'blacklisted' and apt_pkg.version_compare(ver, mi.version) <= 0))]:
                    return True

        return False
