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

from datetime import datetime
import os
import json
import tarfile
import io
import re
import sys
import urllib.parse
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

import apt_pkg
import amqplib.client_0_8 as amqp

import britney2.hints
from britney2.policies.policy import BasePolicy, PolicyVerdict
from britney2.consts import VERSION


EXCUSES_LABELS = {
    "PASS": '<span style="background:#87d96c">Pass</span>',
    "FAIL": '<span style="background:#ff6666">Failed</span>',
    "ALWAYSFAIL": '<span style="background:#e5c545">Always failed</span>',
    "REGRESSION": '<span style="background:#ff6666">Regression</span>',
    "IGNORE-FAIL": '<span style="background:#e5c545">Ignored failure</span>',
    "RUNNING": '<span style="background:#99ddff">Test in progress</span>',
    "RUNNING-ALWAYSFAIL": '<span style="background:#99ddff">Test in progress (always failed)</span>',
}


def srchash(src):
    '''archive hash prefix for source package'''

    if src.startswith('lib'):
        return src[:4]
    else:
        return src[0]


class AutopkgtestPolicy(BasePolicy):
    """autopkgtest regression policy for source migrations

    Run autopkgtests for the excuse and all of its reverse dependencies, and
    reject the upload if any of those regress.
    """

    def __init__(self, options, suite_info):
        super().__init__('autopkgtest', options, suite_info, {'unstable'})
        self.test_state_dir = os.path.join(options.unstable, 'autopkgtest')
        # tests requested in this and previous runs
        # trigger -> src -> [arch]
        self.pending_tests = None
        self.pending_tests_file = os.path.join(self.test_state_dir, 'pending.json')

        # results map: trigger -> src -> arch -> [passed, version, run_id]
        # - trigger is "source/version" of an unstable package that triggered
        #   this test run.
        # - "passed" is a bool
        # - "version" is the package version  of "src" of that test
        # - "run_id" is an opaque ID that identifies a particular test run for
        #   a given src/arch. It's usually a time stamp like "20150120_125959".
        #   This is also used for tracking the latest seen time stamp for
        #   requesting only newer results.
        self.test_results = {}
        if self.options.adt_shared_results_cache:
            self.results_cache_file = self.options.adt_shared_results_cache
        else:
            self.results_cache_file = os.path.join(self.test_state_dir, 'results.cache')

        self.session = requests.Session()
        retry = Retry(total=3, read=3, connect=3)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

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
                self.log("Ignoring ADT_ARCHES %s as it is not in architectures list" % arch)

    def register_hints(self, hint_parser):
        hint_parser.register_hint_type('force-badtest', britney2.hints.split_into_one_hint_per_package)
        hint_parser.register_hint_type('force-reset-test', britney2.hints.split_into_one_hint_per_package)
        hint_parser.register_hint_type('force-skiptest', britney2.hints.split_into_one_hint_per_package)

    def initialise(self, britney):
        super().initialise(britney)
        os.makedirs(self.test_state_dir, exist_ok=True)
        self.read_pending_tests()

        # read the cached results that we collected so far
        if os.path.exists(self.results_cache_file):
            with open(self.results_cache_file) as f:
                self.test_results = json.load(f)
            self.log('Read previous results from %s' % self.results_cache_file)
        else:
            self.log('%s does not exist, re-downloading all results from swift' %
                     self.results_cache_file)

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
            # in production mode, connect to AMQP server
            creds = urllib.parse.urlsplit(amqp_url, allow_fragments=False)
            self.amqp_con = amqp.Connection(creds.hostname, userid=creds.username,
                                            password=creds.password)
            self.amqp_channel = self.amqp_con.channel()
            self.log('Connected to AMQP server')
        elif amqp_url.startswith('file://'):
            # in testing mode, adt_amqp will be a file:// URL
            self.amqp_file = amqp_url[7:]
        else:
            raise RuntimeError('Unknown ADT_AMQP schema %s' % amqp_url.split(':', 1)[0])

    def save_pending_json(self):
        # update the pending tests on-disk cache
        self.log('Updating pending requested tests in %s' % self.pending_tests_file)
        with open(self.pending_tests_file + '.new', 'w') as f:
            json.dump(self.pending_tests, f, indent=2)
        os.rename(self.pending_tests_file + '.new', self.pending_tests_file)

    def save_state(self, britney):
        super().save_state(britney)

        # update the results on-disk cache, unless we are using a r/o shared one
        if not self.options.adt_shared_results_cache:
            self.log('Updating results cache')
            with open(self.results_cache_file + '.new', 'w') as f:
                json.dump(self.test_results, f, indent=2)
            os.rename(self.results_cache_file + '.new', self.results_cache_file)

        self.save_pending_json()

    def apply_policy_impl(self, tests_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        # skip/delay autopkgtests until package is built
        binaries_info = self.britney.sources[suite][source_name]
        unsat_deps = excuse.unsat_deps.copy()
        non_adt_arches = set(self.options.architectures) - set(self.adt_arches)
        interesting_missing_builds = set(excuse.missing_builds) - non_adt_arches
        for arch in set(self.options.break_arches) | non_adt_arches:
            try:
                del unsat_deps[arch]
            except KeyError:
                pass
        if interesting_missing_builds or not binaries_info.binaries or unsat_deps:
            self.log('%s has missing builds or is uninstallable, skipping autopkgtest policy' % excuse.name)
            return PolicyVerdict.REJECTED_TEMPORARILY

        self.log('Checking autopkgtests for %s' % source_name)
        trigger = source_name + '/' + source_data_srcdist.version

        # build a (testsrc, testver) → arch → (status, log_url) map; we trigger/check test
        # results per archtitecture for technical/efficiency reasons, but we
        # want to evaluate and present the results by tested source package
        # first
        pkg_arch_result = {}
        for arch in self.adt_arches:
            # request tests (unless they were already requested earlier or have a result)
            tests = self.tests_for_source(source_name, source_data_srcdist.version, arch)
            is_huge = len(tests) > 20
            for (testsrc, testver) in tests:
                self.pkg_test_request(testsrc, arch, trigger, huge=is_huge)
                (result, real_ver, url) = self.pkg_test_result(testsrc, testver, arch, trigger)
                pkg_arch_result.setdefault((testsrc, real_ver), {})[arch] = (result, url)

        # add test result details to Excuse
        verdict = PolicyVerdict.PASS
        cloud_url = "http://autopkgtest.ubuntu.com/packages/%(h)s/%(s)s/%(r)s/%(a)s"
        for (testsrc, testver) in sorted(pkg_arch_result):
            arch_results = pkg_arch_result[(testsrc, testver)]
            r = set([v[0] for v in arch_results.values()])
            if 'REGRESSION' in r:
                verdict = PolicyVerdict.REJECTED_PERMANENTLY
            elif 'RUNNING' in r and verdict == PolicyVerdict.PASS:
                verdict = PolicyVerdict.REJECTED_TEMPORARILY
            # skip version if still running on all arches
            if not r - {'RUNNING', 'RUNNING-ALWAYSFAIL'}:
                testver = None

            html_archmsg = []
            for arch in sorted(arch_results):
                (status, log_url) = arch_results[arch]
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
                    retry_url = 'https://autopkgtest.ubuntu.com/request.cgi?' + \
                        urllib.parse.urlencode([('release', self.options.series),
                                               ('arch', arch),
                                               ('package', testsrc),
                                               ('trigger', trigger)] +
                                               [('ppa', p) for p in self.options.adt_ppas])
                if testver:
                    testname = '%s/%s' % (testsrc, testver)
                else:
                    testname = testsrc

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

            # render HTML line for testsrc entry
            excuse.addhtml("autopkgtest for %s: %s" % (testname, ', '.join(html_archmsg)))

        if verdict != PolicyVerdict.PASS:
            # check for force-skiptest hint
            hints = self.britney.hints.search('force-skiptest', package=source_name, version=source_data_srcdist.version)
            if hints:
                excuse.addreason('skiptest')
                excuse.addhtml("Should wait for tests relating to %s %s, but forced by %s" %
                               (source_name, source_data_srcdist.version, hints[0].user))
                excuse.force()
                verdict = PolicyVerdict.PASS_HINTED
            else:
                excuse.addreason('autopkgtest')
        return verdict

    #
    # helper functions
    #

    @classmethod
    def has_autodep8(kls, srcinfo, binaries):
        '''Check if package  is covered by autodep8

        srcinfo is an item from self.britney.sources
        binaries is self.britney.binaries['unstable'][arch][0]
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

    def tests_for_source(self, src, ver, arch):
        '''Iterate over all tests that should be run for given source and arch'''

        sources_info = self.britney.sources['testing']
        binaries_info = self.britney.binaries['testing'][arch][0]

        reported_pkgs = set()

        tests = []

        # hack for vivid's gccgo-5 and xenial's gccgo-6; these build libgcc1
        # too, so test some Go and some libgcc1 consumers
        if src in ['gccgo-5', 'gccgo-6']:
            for test in ['juju-mongodb', 'mongodb', 'doxygen']:
                try:
                    tests.append((test, self.britney.sources['testing'][test][VERSION]))
                except KeyError:
                    # no package in that series? *shrug*, then not (mostly for testing)
                    pass
            return tests

        # gcc-N triggers tons of tests via libgcc1, but this is mostly in vain:
        # gcc already tests itself during build, and it is being used from
        # -proposed, so holding it back on a dozen unrelated test failures
        # serves no purpose. Just check some key packages which actually use
        # gcc during the test, and doxygen as an example for a libgcc user.
        if src.startswith('gcc-'):
            if re.match(r'gcc-\d$', src) or src == 'gcc-defaults':
                # add gcc's own tests, if it has any
                srcinfo = self.britney.sources['unstable'][src]
                if 'autopkgtest' in srcinfo.testsuite:
                    tests.append((src, ver))
                for test in ['binutils', 'fglrx-installer', 'doxygen', 'linux']:
                    try:
                        tests.append((test, self.britney.sources['testing'][test][VERSION]))
                    except KeyError:
                        # no package in that series? *shrug*, then not (mostly for testing)
                        pass
                return tests
            else:
                # for other compilers such as gcc-snapshot etc. we don't need
                # to trigger anything
                return []

        # for linux themselves we don't want to trigger tests -- these should
        # all come from linux-meta*. A new kernel ABI without a corresponding
        # -meta won't be installed and thus we can't sensibly run tests against
        # it.
        if src.startswith('linux') and src.replace('linux', 'linux-meta') in self.britney.sources['testing']:
            return []

        # we want to test the package itself, if it still has a test in unstable
        srcinfo = self.britney.sources['unstable'][src]
        test_for_arch = False
        for pkg_id in srcinfo.binaries:
            if pkg_id.architecture in (arch, 'all'):
                test_for_arch = True
        # If the source package builds no binaries for this architecture,
        # don't try to trigger tests for it.
        if not test_for_arch:
            return []

        if 'autopkgtest' in srcinfo.testsuite or self.has_autodep8(srcinfo, binaries_info):
            reported_pkgs.add(src)
            tests.append((src, ver))

        extra_bins = []
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

        # plus all direct reverse dependencies and test triggers of its
        # binaries which have an autopkgtest
        for binary in srcinfo.binaries + extra_bins:
            rdeps = self.britney._inst_tester.reverse_dependencies_of(binary)
            for rdep in rdeps:
                try:
                    rdep_src = binaries_info[rdep.package_name].source
                    # Don't re-trigger the package itself here; this should
                    # have been done above if the package still continues to
                    # have an autopkgtest in unstable.
                    if rdep_src == src:
                        continue
                except KeyError:
                    self.log('%s on %s has no source (NBS?)' % (rdep.package_name, arch))
                    continue

                rdep_src_info = sources_info[rdep_src]
                if 'autopkgtest' in rdep_src_info.testsuite or self.has_autodep8(rdep_src_info, binaries_info):
                    if rdep_src not in reported_pkgs:
                        tests.append((rdep_src, rdep_src_info[VERSION]))
                        reported_pkgs.add(rdep_src)

            for tdep_src in self.britney.testsuite_triggers.get(binary.package_name, set()):
                if tdep_src not in reported_pkgs:
                    try:
                        tdep_src_info = sources_info[tdep_src]
                    except KeyError:
                        continue
                    if 'autopkgtest' in tdep_src_info.testsuite or self.has_autodep8(tdep_src_info, binaries_info):
                        for pkg_id in tdep_src_info.binaries:
                            if pkg_id.architecture == arch:
                                tests.append((tdep_src, tdep_src_info[VERSION]))
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
                                tests.append((pkg, self.britney.sources['unstable'][pkg][VERSION]))
                            except KeyError:
                                try:
                                    tests.append((pkg, self.britney.sources['testing'][pkg][VERSION]))
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
            self.log('No %s, starting with no pending tests' %
                     self.pending_tests_file)
            self.pending_tests = {}
            return
        with open(self.pending_tests_file) as f:
            self.pending_tests = json.load(f)
        self.log('Read pending requested tests from %s: %s' %
                 (self.pending_tests_file, self.pending_tests))

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
        self.latest_run_for_package._cache.setdefault(src, {})[arch] = latest_run_id
        return latest_run_id

    latest_run_for_package._cache = {}

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

        resp = self.session.get(url, timeout=30)
        if resp.status_code == 200:
            result_paths = resp.text.strip().splitlines()
        elif resp.status_code == 204:  # No content
            result_paths = []
        elif resp.status_code == 401:
            # 401 "Unauthorized" is swift's way of saying "container does not exist"
            self.log('fetch_swift_results: %s does not exist yet or is inaccessible' % url)
            return
        else:
            # Other status codes are usually a transient
            # network/infrastructure failure. Ignoring this can lead to
            # re-requesting tests which we already have results for, so
            # fail hard on this and let the next run retry.
            self.log('FATAL: Failure to fetch swift results from %s: got error code %s' % (url, resp.status_code))
            sys.exit(1)

        for p in result_paths:
            self.fetch_one_result(
                os.path.join(swift_url, self.swift_container, p, 'result.tar'), src, arch)

    fetch_swift_results._done = set()

    def fetch_one_result(self, url, src, arch):
        '''Download one result URL for source/arch

        Remove matching pending_tests entries.
        '''
        resp = self.session.get(url, timeout=30)
        if resp.status_code == 200:
            tar_bytes = io.BytesIO(resp.content)
        # we tolerate "not found" (something went wrong on uploading the
        # result), but other things indicate infrastructure problems
        elif resp.status_code == 404:
            return
        else:
            self.log('Failure to fetch %s: error code %s' % (url, resp.status_code))
            sys.exit(1)

        try:
            with tarfile.open(None, 'r', tar_bytes) as tar:
                exitcode = int(tar.extractfile('exitcode').read().strip())
                try:
                    srcver = tar.extractfile('testpkg-version').read().decode().strip()
                except KeyError as e:
                    if exitcode in (4, 12, 20):
                        # repair it
                        srcver = "%s unknown" % (src)
                    else:
                        raise
                (ressrc, ver) = srcver.split()
                testinfo = json.loads(tar.extractfile('testinfo.json').read().decode())
        except (KeyError, ValueError, tarfile.TarError) as e:
            self.log('%s is damaged, ignoring: %s' % (url, str(e)), 'E')
            # ignore this; this will leave an orphaned request in pending.json
            # and thus require manual retries after fixing the tmpfail, but we
            # can't just blindly attribute it to some pending test.
            return

        if src != ressrc:
            self.log('%s is a result for package %s, but expected package %s' %
                     (url, ressrc, src), 'E')
            return

        # parse recorded triggers in test result
        for e in testinfo.get('custom_environment', []):
            if e.startswith('ADT_TEST_TRIGGERS='):
                result_triggers = [i for i in e.split('=', 1)[1].split() if '/' in i]
                break
        else:
            self.log('%s result has no ADT_TEST_TRIGGERS, ignoring', 'E')
            return

        stamp = os.path.basename(os.path.dirname(url))
        # allow some skipped tests, but nothing else
        passed = exitcode in [0, 2, 8]

        self.log('Fetched test result for %s/%s/%s %s (triggers: %s): %s' % (
            src, ver, arch, stamp, result_triggers, passed and 'pass' or 'fail'))

        # remove matching test requests
        for trigger in result_triggers:
            try:
                arch_list = self.pending_tests[trigger][src]
                arch_list.remove(arch)
                if not arch_list:
                    del self.pending_tests[trigger][src]
                if not self.pending_tests[trigger]:
                    del self.pending_tests[trigger]
                self.log('-> matches pending request %s/%s for trigger %s' % (src, arch, trigger))
            except (KeyError, ValueError):
                self.log('-> does not match any pending request for %s/%s' % (src, arch))

        # add this result
        for trigger in result_triggers:
            # If a test runs because of its own package (newer version), ensure
            # that we got a new enough version; FIXME: this should be done more
            # generically by matching against testpkg-versions
            (trigsrc, trigver) = trigger.split('/', 1)
            if trigsrc == src and apt_pkg.version_compare(ver, trigver) < 0:
                self.log('test trigger %s, but run for older version %s, ignoring' % (trigger, ver), 'E')
                continue

            result = self.test_results.setdefault(trigger, {}).setdefault(
                src, {}).setdefault(arch, [False, None, ''])

            # don't clobber existing passed results with failures from re-runs
            if passed or not result[0]:
                result[0] = passed
                result[1] = ver
                result[2] = stamp

    def send_test_request(self, src, arch, trigger, huge=False):
        '''Send out AMQP request for testing src/arch for trigger

        If huge is true, then the request will be put into the -huge instead of
        normal queue.
        '''
        if self.options.dry_run:
            return

        params = {'triggers': [trigger]}
        if self.options.adt_ppas:
            params['ppas'] = self.options.adt_ppas
            qname = 'debci-ppa-%s-%s' % (self.options.series, arch)
        elif huge:
            qname = 'debci-huge-%s-%s' % (self.options.series, arch)
        else:
            qname = 'debci-%s-%s' % (self.options.series, arch)
        params['submit-time'] = datetime.strftime(datetime.utcnow(), '%Y-%m-%d %H:%M:%S%z')
        params = json.dumps(params)

        if self.amqp_channel:
            self.amqp_channel.basic_publish(amqp.Message(src + '\n' + params,
                                                         delivery_mode=2),  # persistent
                                            routing_key=qname)
        else:
            assert self.amqp_file
            with open(self.amqp_file, 'a') as f:
                f.write('%s:%s %s\n' % (qname, src, params))

    def pkg_test_request(self, src, arch, trigger, huge=False):
        '''Request one package test for one particular trigger

        trigger is "pkgname/version" of the package that triggers the testing
        of src. If huge is true, then the request will be put into the -huge
        instead of normal queue.

        This will only be done if that test wasn't already requested in a
        previous run (i. e. not already in self.pending_tests) or there already
        is a result for it. This ensures to download current results for this
        package before requesting any test.
        '''
        # Don't re-request if we already have a result
        try:
            passed = self.test_results[trigger][src][arch][0]
            if passed:
                self.log('%s/%s triggered by %s already passed' % (src, arch, trigger))
                return
            self.log('Checking for new results for failed %s/%s for trigger %s' %
                     (src, arch, trigger))
            raise KeyError  # fall through
        except KeyError:
            self.fetch_swift_results(self.options.adt_swift_url, src, arch)
            # do we have one now?
            try:
                self.test_results[trigger][src][arch]
                return
            except KeyError:
                pass

        # Don't re-request if it's already pending
        arch_list = self.pending_tests.setdefault(trigger, {}).setdefault(src, [])
        if arch in arch_list:
            self.log('Test %s/%s for %s is already pending, not queueing' %
                     (src, arch, trigger))
        else:
            self.log('Requesting %s autopkgtest on %s to verify %s' %
                     (src, arch, trigger))
            arch_list.append(arch)
            arch_list.sort()
            self.send_test_request(src, arch, trigger, huge=huge)
            # save pending.json right away, so that we don't re-request if britney crashes
            self.save_pending_json()

    def check_ever_passed_before(self, src, max_ver, arch, min_ver=None):
        '''Check if tests for src ever passed on arch for specified range

        If min_ver is specified, it checks that all versions in
        [min_ver, max_ver) have passed; otherwise it checks that
        [min_ver, inf) have passed.'''

        # FIXME: add caching
        for srcmap in self.test_results.values():
            try:
                too_high = apt_pkg.version_compare(srcmap[src][arch][1], max_ver) > 0
                too_low = apt_pkg.version_compare(srcmap[src][arch][1], min_ver) <= 0 if min_ver else False

                if too_high or too_low:
                    continue

                if srcmap[src][arch][0]:
                    return True
            except KeyError:
                pass
        return False

    def pkg_test_result(self, src, ver, arch, trigger):
        '''Get current test status of a particular package

        Return (status, real_version, log_url) tuple; status is a key in
        EXCUSES_LABELS. log_url is None if the test is still running.
        '''
        # determine current test result status
        until = self.find_max_lower_force_reset_test(src, ver, arch)
        ever_passed = self.check_ever_passed_before(src, ver, arch, until)
        url = None
        try:
            r = self.test_results[trigger][src][arch]
            ver = r[1]
            run_id = r[2]
            if r[0]:
                result = 'PASS'
            else:
                # Special-case triggers from linux-meta*: we cannot compare
                # results against different kernels, as e. g. a DKMS module
                # might work against the default kernel but fail against a
                # different flavor; so for those, ignore the "ever
                # passed" check; FIXME: check against trigsrc only
                if trigger.startswith('linux-meta') or trigger.startswith('linux/'):
                    ever_passed = False

                if ever_passed:
                    if self.has_force_badtest(src, ver, arch):
                        result = 'IGNORE-FAIL'
                    elif self.has_higher_force_reset_test(src, ver, arch):
                        # we've got a force-reset-test foo/N, N >= ver hint;
                        # this is ALWAYSFAIL
                        result = 'ALWAYSFAIL'
                    else:
                        result = 'REGRESSION'
                else:
                    result = 'ALWAYSFAIL'

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
                if ever_passed and not self.has_force_badtest(src, ver, arch):
                    result = 'RUNNING'
                else:
                    result = 'RUNNING-ALWAYSFAIL'
                url = 'http://autopkgtest.ubuntu.com/running'
            else:
                raise RuntimeError('Result for %s/%s/%s (triggered by %s) is neither known nor pending!' %
                                   (src, ver, arch, trigger))

        return (result, ver, url)

    def find_max_lower_force_reset_test(self, src, ver, arch):
        '''Find the maximum force-reset-test hint before/including ver'''
        hints = self.britney.hints.search('force-reset-test', package=src)
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
        hints = self.britney.hints.search('force-reset-test', package=src)

        if hints:
            self.log('Checking hints for %s/%s/%s: %s' % (src, ver, arch, [str(h) for h in hints]))
            for hint in hints:
                for mi in hint.packages:
                    if (mi.architecture in ['source', arch] and
                            mi.version != 'all' and
                            apt_pkg.version_compare(mi.version, ver) >= 0):
                        return True

        return False

    def has_force_badtest(self, src, ver, arch):
        '''Check if src/ver/arch has a force-badtest hint'''

        hints = self.britney.hints.search('force-badtest', package=src)
        if hints:
            self.log('Checking hints for %s/%s/%s: %s' % (src, ver, arch, [str(h) for h in hints]))
            for hint in hints:
                if [mi for mi in hint.packages if mi.architecture in ['source', arch] and
                        (mi.version == 'all' or
                         (mi.version == 'blacklisted' and ver == 'blacklisted') or
                         (mi.version != 'blacklisted' and apt_pkg.version_compare(ver, mi.version) <= 0))]:
                    return True

        return False
