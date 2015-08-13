# -*- coding: utf-8 -*-

# Copyright (C) 2013 Canonical Ltd.
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

from __future__ import print_function

import os
import time
import json
import tarfile
import io
import copy
import itertools
from urllib import urlencode, urlopen

import apt_pkg
import kombu

from consts import (AUTOPKGTEST, BINARIES, DEPENDS, RDEPENDS, SOURCE, VERSION)


ADT_EXCUSES_LABELS = {
    "PASS": '<span style="background:#87d96c">Pass</span>',
    "ALWAYSFAIL": '<span style="background:#e5c545">Always failed</span>',
    "REGRESSION": '<span style="background:#ff6666">Regression</span>',
    "RUNNING": '<span style="background:#99ddff">Test in progress</span>',
}


def srchash(src):
    '''archive hash prefix for source package'''

    if src.startswith('lib'):
        return src[:4]
    else:
        return src[0]


def merge_triggers(trigs1, trigs2):
    '''Merge two (pkg, ver) trigger iterables

    Return [(pkg, ver), ...] list with only the highest version for each
    package.
    '''
    pkgvers = {}
    for pkg, ver in itertools.chain(trigs1, trigs2):
        if apt_pkg.version_compare(ver, pkgvers.setdefault(pkg, '0')) >= 0:
            pkgvers[pkg] = ver
    return list(pkgvers.items())


class AutoPackageTest(object):
    """autopkgtest integration

    Look for autopkgtest jobs to run for each update that is otherwise a
    valid candidate, and collect the results.  If an update causes any
    autopkgtest jobs to be run, then they must all pass before the update is
    accepted.
    """

    def __init__(self, britney, distribution, series, debug=False):
        self.britney = britney
        self.distribution = distribution
        self.series = series
        self.debug = debug
        self.excludes = set()
        self.test_state_dir = os.path.join(britney.options.unstable,
                                           'autopkgtest')
        # map of requested tests from request()
        # src -> ver -> arch -> {(triggering-src1, ver1), ...}
        self.requested_tests = {}
        # same map for tests requested in previous runs
        self.pending_tests = None
        self.pending_tests_file = os.path.join(self.test_state_dir, 'pending.txt')

        if not os.path.isdir(self.test_state_dir):
            os.mkdir(self.test_state_dir)
        self.read_pending_tests()

        # results map: src -> arch -> [latest_stamp, ver -> (passed, triggers), ever_passed]
        # - "passed" is a bool
        # - It's tempting to just use a global "latest" time stamp, but due to
        #   swift's "eventual consistency" we might miss results with older time
        #   stamps from other packages that we don't see in the current run, but
        #   will in the next one. This doesn't hurt for older results of the same
        #   package.
        # - triggers is a list of (source, version) pairs which unstable
        #   packages triggered this test run. We need to track this to avoid
        #   unnecessarily re-running tests.
        # - ever_passed is a bool whether there is any successful test of
        #   src/arch of any version. This is used for detecting "regression"
        #   vs. "always failed"
        self.test_results = {}
        self.results_cache_file = os.path.join(self.test_state_dir, 'results.cache')

        # read the cached results that we collected so far
        if os.path.exists(self.results_cache_file):
            with open(self.results_cache_file) as f:
                self.test_results = json.load(f)
            self.log_verbose('Read previous results from %s' % self.results_cache_file)
        else:
            self.log_verbose('%s does not exist, re-downloading all results '
                             'from swift' % self.results_cache_file)

    def log_verbose(self, msg):
        if self.britney.options.verbose:
            print('I: [%s] - %s' % (time.asctime(), msg))

    def log_error(self, msg):
        print('E: [%s] - %s' % (time.asctime(), msg))

    def has_autodep8(self, srcinfo):
        '''Check if package  is covered by autodep8

        srcinfo is an item from self.britney.sources
        '''
        # DKMS: some binary depends on "dkms"
        for bin_arch in srcinfo[BINARIES]:
            binpkg = bin_arch.split('/')[0]  # chop off arch
            try:
                bininfo = self.britney.binaries['unstable']['amd64'][0][binpkg]
            except KeyError:
                continue
            if 'dkms' in (bininfo[DEPENDS] or ''):
                return True
        return False

    def tests_for_source(self, src, ver):
        '''Iterate over all tests that should be run for given source'''

        sources_info = self.britney.sources['unstable']
        # FIXME: For now assume that amd64 has all binaries that we are
        # interested in for reverse dependency checking
        binaries_info = self.britney.binaries['unstable']['amd64'][0]

        reported_pkgs = set()

        tests = []

        srcinfo = sources_info[src]
        # we want to test the package itself, if it still has a test in
        # unstable
        if srcinfo[AUTOPKGTEST] or self.has_autodep8(srcinfo):
            reported_pkgs.add(src)
            tests.append((src, ver))

        # plus all direct reverse dependencies of its binaries which have
        # an autopkgtest
        for binary in srcinfo[BINARIES]:
            binary = binary.split('/')[0]  # chop off arch
            try:
                rdeps = binaries_info[binary][RDEPENDS]
            except KeyError:
                self.log_verbose('Ignoring nonexistant binary %s (FTBFS/NBS)?' % binary)
                continue
            for rdep in rdeps:
                rdep_src = binaries_info[rdep][SOURCE]
                # if rdep_src/unstable is known to be not built yet or
                # uninstallable, try to run tests against testing; if that
                # works, then the unstable src does not break the testing
                # rdep_src and is fine
                if rdep_src in self.excludes:
                    try:
                        rdep_src_info = self.britney.sources['testing'][rdep_src]
                        self.log_verbose('Reverse dependency %s of %s/%s is unbuilt or uninstallable, running test against testing version %s' %
                                         (rdep_src, src, ver, rdep_src_info[VERSION]))
                    except KeyError:
                        self.log_verbose('Reverse dependency %s of %s/%s is unbuilt or uninstallable and not present in testing, ignoring' %
                                         (rdep_src, src, ver))
                        continue
                else:
                    rdep_src_info = sources_info[rdep_src]
                if rdep_src_info[AUTOPKGTEST] or self.has_autodep8(rdep_src_info):
                    if rdep_src not in reported_pkgs:
                        tests.append((rdep_src, rdep_src_info[VERSION]))
                        reported_pkgs.add(rdep_src)

        tests.sort(key=lambda (s, v): s)
        return tests

    #
    # AMQP/cloud interface helpers
    #

    def read_pending_tests(self):
        '''Read pending test requests from previous britney runs

        Read UNSTABLE/autopkgtest/requested.txt with the format:
            srcpkg srcver triggering-srcpkg triggering-srcver

        Initialize self.pending_tests with that data.
        '''
        assert self.pending_tests is None, 'already initialized'
        self.pending_tests = {}
        if not os.path.exists(self.pending_tests_file):
            self.log_verbose('No %s, starting with no pending tests' %
                             self.pending_tests_file)
            return
        with open(self.pending_tests_file) as f:
            for l in f:
                l = l.strip()
                if not l:
                    continue
                try:
                    (src, ver, arch, trigsrc, trigver) = l.split()
                except ValueError:
                    self.log_error('ignoring malformed line in %s: %s' %
                                   (self.pending_tests_file, l))
                    continue
                self.pending_tests.setdefault(src, {}).setdefault(
                    ver, {}).setdefault(arch, set()).add((trigsrc, trigver))
        self.log_verbose('Read pending requested tests from %s: %s' %
                         (self.pending_tests_file, self.pending_tests))

    def update_pending_tests(self):
        '''Update pending tests after submitting requested tests

        Update UNSTABLE/autopkgtest/requested.txt, see read_pending_tests() for
        the format.
        '''
        # merge requested_tests into pending_tests
        for src, verinfo in self.requested_tests.items():
            for ver, archinfo in verinfo.items():
                for arch, triggers in archinfo.items():
                    self.pending_tests.setdefault(src, {}).setdefault(
                        ver, {}).setdefault(arch, set()).update(triggers)
        self.requested_tests = {}

        # write it
        with open(self.pending_tests_file + '.new', 'w') as f:
            for src in sorted(self.pending_tests):
                for ver in sorted(self.pending_tests[src]):
                    for arch in sorted(self.pending_tests[src][ver]):
                        for (trigsrc, trigver) in sorted(self.pending_tests[src][ver][arch]):
                            f.write('%s %s %s %s %s\n' % (src, ver, arch, trigsrc, trigver))
        os.rename(self.pending_tests_file + '.new', self.pending_tests_file)
        self.log_verbose('Updated pending requested tests in %s' %
                         self.pending_tests_file)

    def add_test_request(self, src, ver, arch, trigsrc, trigver):
        '''Add one test request to the local self.requested_tests queue

        This will only be done if that test wasn't already requested in a
        previous run (i. e. not already in self.pending_tests) or there already
        is a result for it.
        '''
        try:
            for (tsrc, tver) in self.test_results[src][arch][1][ver][1]:
                if tsrc == trigsrc and apt_pkg.version_compare(tver, trigver) >= 0:
                    self.log_verbose('There already is a result for %s/%s/%s triggered by %s/%s' %
                                     (src, ver, arch, tsrc, tver))
                    return
        except KeyError:
            pass

        if (trigsrc, trigver) in self.pending_tests.get(src, {}).get(
                ver, {}).get(arch, set()):
            self.log_verbose('test %s/%s/%s for %s/%s is already pending, not queueing' %
                             (src, ver, arch, trigsrc, trigver))
            return
        self.requested_tests.setdefault(src, {}).setdefault(
            ver, {}).setdefault(arch, set()).add((trigsrc, trigver))

    def fetch_swift_results(self, swift_url, src, arch, trigger=None):
        '''Download new results for source package/arch from swift'''

        # prepare query: get all runs with a timestamp later than latest_stamp
        # for this package/arch; '@' is at the end of each run timestamp, to
        # mark the end of a test run directory path
        # example: <autopkgtest-wily>wily/amd64/libp/libpng/20150630_054517@/result.tar
        query = {'delimiter': '@',
                 'prefix': '%s/%s/%s/%s/' % (self.series, arch, srchash(src), src)}
        try:
            query['marker'] = query['prefix'] + self.test_results[src][arch][0]
        except KeyError:
            # no stamp yet, download all results
            pass

        # request new results from swift
        url = os.path.join(swift_url, 'autopkgtest-' + self.series)
        url += '?' + urlencode(query)
        try:
            f = urlopen(url)
            if f.getcode() == 200:
                result_paths = f.read().strip().splitlines()
            elif f.getcode() == 204:  # No content
                result_paths = []
            else:
                self.log_error('Failure to fetch swift results from %s: %u' %
                               (url, f.getcode()))
                f.close()
                return
            f.close()
        except IOError as e:
            self.log_error('Failure to fetch swift results from %s: %s' % (url, str(e)))
            return

        for p in result_paths:
            self.fetch_one_result(
                os.path.join(swift_url, 'autopkgtest-' + self.series, p, 'result.tar'),
                src, arch, trigger)

    def fetch_one_result(self, url, src, arch, trigger=None):
        '''Download one result URL for source/arch

        Remove matching pending_tests entries. If trigger is given (src, ver)
        it is added to the triggers of that result.
        '''
        try:
            f = urlopen(url)
            if f.getcode() == 200:
                tar_bytes = io.BytesIO(f.read())
                f.close()
            else:
                self.log_error('Failure to fetch %s: %u' % (url, f.getcode()))
                return
        except IOError as e:
            self.log_error('Failure to fetch %s: %s' % (url, str(e)))
            return

        try:
            with tarfile.open(None, 'r', tar_bytes) as tar:
                exitcode = int(tar.extractfile('exitcode').read().strip())
                srcver = tar.extractfile('testpkg-version').read().decode().strip()
                (ressrc, ver) = srcver.split()
        except (KeyError, ValueError, tarfile.TarError) as e:
            self.log_error('%s is damaged: %s' % (url, str(e)))
            # we can't just ignore this, as it would leave an orphaned request
            # in pending.txt; consider it tmpfail
            exitcode = 16
            ressrc = src
            ver = None

        if src != ressrc:
            self.log_error('%s is a result for package %s, but expected package %s' %
                           (url, ressrc, src))
            return

        stamp = os.path.basename(os.path.dirname(url))
        # allow some skipped tests, but nothing else
        passed = exitcode in [0, 2]

        self.log_verbose('Fetched test result for %s/%s/%s %s: %s' % (
            src, ver, arch, stamp, passed and 'pass' or 'fail'))

        # remove matching test requests, remember triggers
        satisfied_triggers = set()
        for pending_ver, pending_archinfo in self.pending_tests.get(src, {}).copy().items():
            # if we encounter a tmpfail above, attribute it to the pending test
            if ver is None:
                ver = pending_ver
            # don't consider newer requested versions
            if apt_pkg.version_compare(pending_ver, ver) <= 0:
                try:
                    t = pending_archinfo[arch]
                    self.log_verbose('-> matches pending request for triggers %s' % str(t))
                    satisfied_triggers.update(t)
                    del self.pending_tests[src][pending_ver][arch]
                except KeyError:
                    self.log_error('-> does not match any pending request!')
                    pass
        if trigger:
            satisfied_triggers.add(trigger)

        # add this result
        src_arch_results = self.test_results.setdefault(src, {}).setdefault(arch, [stamp, {}, False])
        if passed:
            # update ever_passed field
            src_arch_results[2] = True
        src_arch_results[1][ver] = (passed, merge_triggers(
            src_arch_results[1].get(ver, (None, []))[1], satisfied_triggers))
        # update latest_stamp
        if stamp > src_arch_results[0]:
            src_arch_results[0] = stamp

    def failed_tests_for_trigger(self, trigsrc, trigver):
        '''Return (src, arch) set for failed tests for given trigger pkg'''

        result = set()
        for src, srcinfo in self.test_results.iteritems():
            for arch, (stamp, vermap, ever_passed) in srcinfo.iteritems():
                for ver, (passed, triggers) in vermap.iteritems():
                    if not passed:
                        # triggers might contain tuples or lists (after loading
                        # from json), so iterate/check manually
                        for s, v in triggers:
                            if trigsrc == s and trigver == v:
                                result.add((src, arch))
        return result

    #
    # Public API
    #

    def request(self, packages, excludes=None):
        if excludes:
            self.excludes.update(excludes)

        self.log_verbose('Requested autopkgtests for %s, exclusions: %s' %
                         (['%s/%s' % i for i in packages], str(self.excludes)))
        for src, ver in packages:
            for (testsrc, testver) in self.tests_for_source(src, ver):
                if testsrc not in self.excludes:
                    for arch in self.britney.options.adt_arches.split():
                        self.add_test_request(testsrc, testver, arch, src, ver)

        if self.britney.options.verbose:
            for src, verinfo in self.requested_tests.items():
                for ver, archinfo in verinfo.items():
                    for arch, triggers in archinfo.items():
                        self.log_verbose('Requesting %s/%s/%s autopkgtest to verify %s' %
                                         (src, ver, arch, ', '.join(['%s/%s' % i for i in triggers])))

    def submit(self):
        # send AMQP requests for new test requests
        # TODO: Once we support version constraints in AMQP requests, add them
        arch_queues = {}
        for arch in self.britney.options.adt_arches.split():
            arch_queues[arch] = 'debci-%s-%s' % (self.series, arch)

        try:
            amqp_url = self.britney.options.adt_amqp
        except AttributeError:
            self.log_error('ADT_AMQP not set, cannot submit requests')
            return

        def _arches(verinfo):
            res = set()
            for v, archinfo in verinfo.items():
                res.update(archinfo.keys())
            return res

        if amqp_url.startswith('amqp://'):
            with kombu.Connection(amqp_url) as conn:
                for arch in arch_queues:
                    # don't use SimpleQueue here as it always declares queues;
                    # ACLs might not allow that
                    with kombu.Producer(conn, routing_key=arch_queues[arch], auto_declare=False) as p:
                        for pkg, verinfo in self.requested_tests.items():
                            if arch in _arches(verinfo):
                                p.publish(pkg)
        elif amqp_url.startswith('file://'):
            # in testing mode, adt_amqp will be a file:// URL
            with open(amqp_url[7:], 'a') as f:
                for pkg, verinfo in self.requested_tests.items():
                    for arch in _arches(verinfo):
                        f.write('%s:%s\n' % (arch_queues[arch], pkg))
        else:
            self.log_error('Unknown ADT_AMQP schema in %s' %
                           self.britney.options.adt_amqp)

        # mark them as pending now
        self.update_pending_tests()

    def collect(self, packages):
        # fetch results from swift
        try:
            swift_url = self.britney.options.adt_swift_url
        except AttributeError:
            self.log_error('ADT_SWIFT_URL not set, cannot collect results')
            return
        try:
            self.britney.options.adt_amqp
        except AttributeError:
            self.log_error('ADT_AMQP not set, not collecting results from swift')
            return

        # update results from swift for all packages that we are waiting
        # for, and remove pending tests that we have results for on all
        # arches
        for pkg, verinfo in copy.deepcopy(self.pending_tests.items()):
            for archinfo in verinfo.values():
                for arch in archinfo:
                    self.fetch_swift_results(swift_url, pkg, arch)
        # also update results for excuses whose tests failed, in case a
        # manual retry worked
        for (trigpkg, trigver) in packages:
            if trigpkg not in self.pending_tests:
                for (pkg, arch) in self.failed_tests_for_trigger(trigpkg, trigver):
                    self.log_verbose('Checking for new results for failed %s on %s for trigger %s/%s' %
                                     (pkg, arch, trigpkg, trigver))
                    self.fetch_swift_results(swift_url, pkg, arch, (trigpkg, trigver))

        # update the results cache
        with open(self.results_cache_file + '.new', 'w') as f:
            json.dump(self.test_results, f, indent=2)
        os.rename(self.results_cache_file + '.new', self.results_cache_file)
        self.log_verbose('Updated results cache')

        # new results remove pending requests, update the on-disk cache
        self.update_pending_tests()

    def results(self, trigsrc, trigver):
        '''Return test results for triggering package

        Return (passed, src, ver, arch -> ALWAYSFAIL|PASS|FAIL|RUNNING)
        iterator for all package tests that got triggered by trigsrc/trigver.
        '''
        for testsrc, testver in self.tests_for_source(trigsrc, trigver):
            passed = True
            arch_status = {}
            for arch in self.britney.options.adt_arches.split():
                try:
                    (_, ver_map, ever_passed) = self.test_results[testsrc][arch]
                    (status, triggers) = ver_map[testver]
                    # triggers might contain tuples or lists
                    if (trigsrc, trigver) not in triggers and [trigsrc, trigver] not in triggers:
                        raise KeyError('No result for trigger %s/%s yet' % (trigsrc, trigver))
                    if status:
                        arch_status[arch] = 'PASS'
                    else:
                        # test failed, check ever_passed flag for that src/arch
                        if ever_passed:
                            arch_status[arch] = 'REGRESSION'
                            passed = False
                        else:
                            arch_status[arch] = 'ALWAYSFAIL'
                except KeyError:
                    # no result for testsrc/testver/arch; still running?
                    try:
                        self.pending_tests[testsrc][testver][arch]
                        arch_status[arch] = 'RUNNING'
                        passed = False
                    except KeyError:
                        # ignore if adt or swift results are disabled,
                        # otherwise this is unexpected
                        if not hasattr(self.britney.options, 'adt_swift_url'):
                            continue
                        # FIXME: Ignore this error for now as it crashes britney, but investigate!
                        self.log_error('FIXME: Result for %s/%s/%s (triggered by %s/%s) is neither known nor pending!' %
                                       (testsrc, testver, arch, trigsrc, trigver))
                        continue

            # disabled or ignored?
            if not arch_status:
                continue

            yield (passed, testsrc, testver, arch_status)
