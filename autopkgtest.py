# -*- coding: utf-8 -*-

# Copyright (C) 2013 - 2015 Canonical Ltd.
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

import os
import time
import json
import tarfile
import io
import copy
import re
from urllib.parse import urlencode
from urllib.request import urlopen

import apt_pkg
import kombu

from consts import (AUTOPKGTEST, BINARIES, DEPENDS, RDEPENDS, SOURCE, VERSION)


def srchash(src):
    '''archive hash prefix for source package'''

    if src.startswith('lib'):
        return src[:4]
    else:
        return src[0]


def latest_item(ver_map, min_version=None):
    '''Return (ver, value) from version -> value map with latest version number

    If min_version is given, version has to be >= that, otherwise a KeyError is
    raised.
    '''
    latest = None
    for ver in ver_map:
        if latest is None or apt_pkg.version_compare(ver, latest) > 0:
            latest = ver
    if min_version is not None and latest is not None and \
       apt_pkg.version_compare(latest, min_version) < 0:
        latest = None

    if latest is not None:
        return (latest, ver_map[latest])
    else:
        raise KeyError('no version >= %s' % min_version)


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

        # results map: src -> arch -> [latest_stamp, ver -> trigger -> passed, ever_passed]
        # - It's tempting to just use a global "latest" time stamp, but due to
        #   swift's "eventual consistency" we might miss results with older time
        #   stamps from other packages that we don't see in the current run, but
        #   will in the next one. This doesn't hurt for older results of the same
        #   package.
        # - trigger is "source/version" of an unstable package that triggered
        #   this test run. We need to track this to avoid unnecessarily
        #   re-running tests.
        # - "passed" is a bool
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

    @classmethod
    def has_autodep8(kls, srcinfo, binaries):
        '''Check if package  is covered by autodep8

        srcinfo is an item from self.britney.sources
        binaries is self.britney.binaries['unstable'][arch][0]
        '''
        # DKMS: some binary depends on "dkms"
        for bin_arch in srcinfo[BINARIES]:
            binpkg = bin_arch.split('/')[0]  # chop off arch
            try:
                bininfo = binaries[binpkg]
            except KeyError:
                continue
            if 'dkms' in (bininfo[DEPENDS] or ''):
                return True
        return False

    def tests_for_source(self, src, ver, arch):
        '''Iterate over all tests that should be run for given source and arch'''

        sources_info = self.britney.sources['unstable']
        binaries_info = self.britney.binaries['unstable'][arch][0]

        reported_pkgs = set()

        tests = []

        # hack for vivid's gccgo-5
        if src == 'gccgo-5':
            for test in ['juju', 'juju-core', 'juju-mongodb', 'mongodb']:
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
        # gcc during the test, and libreoffice as an example for a libgcc user.
        if src.startswith('gcc-'):
            if re.match('gcc-\d$', src):
                for test in ['binutils', 'fglrx-installer', 'libreoffice', 'linux']:
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

        srcinfo = sources_info[src]
        # we want to test the package itself, if it still has a test in
        # unstable
        if srcinfo[AUTOPKGTEST] or self.has_autodep8(srcinfo, binaries_info):
            reported_pkgs.add(src)
            tests.append((src, ver))

        extra_bins = []
        # Hack: For new kernels trigger all DKMS packages by pretending that
        # linux-meta* builds a "dkms" binary as well. With that we ensure that we
        # don't regress DKMS drivers with new kernel versions.
        if src.startswith('linux-meta'):
            # does this have any image on this arch?
            for b in srcinfo[BINARIES]:
                p, a = b.split('/', 1)
                if a == arch and '-image' in p:
                    extra_bins.append('dkms')

        # plus all direct reverse dependencies of its binaries which have
        # an autopkgtest
        for binary in srcinfo[BINARIES] + extra_bins:
            binary = binary.split('/')[0]  # chop off arch
            try:
                rdeps = binaries_info[binary][RDEPENDS]
            except KeyError:
                self.log_verbose('Ignoring nonexistant binary %s on %s (FTBFS/NBS)?' % (binary, arch))
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
                if rdep_src_info[AUTOPKGTEST] or self.has_autodep8(rdep_src_info, binaries_info):
                    if rdep_src not in reported_pkgs:
                        tests.append((rdep_src, rdep_src_info[VERSION]))
                        reported_pkgs.add(rdep_src)

        # Hardcode linux-meta →  linux, lxc, glibc, systemd triggers until we get a more flexible
        # implementation: https://bugs.debian.org/779559
        if src.startswith('linux-meta'):
            for pkg in ['lxc', 'glibc', src.replace('linux-meta', 'linux'), 'systemd']:
                if pkg not in reported_pkgs:
                    # does this have any image on this arch?
                    for b in srcinfo[BINARIES]:
                        p, a = b.split('/', 1)
                        if a == arch and '-image' in p:
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
        # check for existing results for both the requested and the current
        # unstable version: test runs might see newly built versions which we
        # didn't see in britney yet
        ver_trig_results = self.test_results.get(src, {}).get(arch, [None, {}, None])[1]
        unstable_ver = self.britney.sources['unstable'][src][VERSION]
        try:
            testing_ver = self.britney.sources['testing'][src][VERSION]
        except KeyError:
            testing_ver = unstable_ver
        for result_ver in set([testing_ver, ver, unstable_ver]):
            # result_ver might be < ver here; that's okay, if we already have a
            # result for trigsrc/trigver we don't need to re-run it again
            if result_ver not in ver_trig_results:
                continue
            for trigger in ver_trig_results[result_ver]:
                (tsrc, tver) = trigger.split('/', 1)
                if tsrc == trigsrc and apt_pkg.version_compare(tver, trigver) >= 0:
                    self.log_verbose('There already is a result for %s/%s/%s triggered by %s/%s' %
                                     (src, result_ver, arch, tsrc, tver))
                    return

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
                result_paths = f.read().decode().strip().splitlines()
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
                try:
                    testinfo = json.loads(tar.extractfile('testinfo.json').read().decode())
                except KeyError:
                    self.log_error('warning: %s does not have a testinfo.json' % url)
                    testinfo = {}
        except (KeyError, ValueError, tarfile.TarError) as e:
            self.log_error('%s is damaged, ignoring: %s' % (url, str(e)))
            # ignore this; this will leave an orphaned request in pending.txt
            # and thus require manual retries after fixing the tmpfail, but we
            # can't just blindly attribute it to some pending test.
            return

        if src != ressrc:
            self.log_error('%s is a result for package %s, but expected package %s' %
                           (url, ressrc, src))
            return

        # parse recorded triggers in test result
        if 'custom_environment' in testinfo:
            for e in testinfo['custom_environment']:
                if e.startswith('ADT_TEST_TRIGGERS='):
                    result_triggers = [tuple(i.split('/', 1)) for i in e.split('=', 1)[1].split() if '/' in i]
                    break
        else:
            result_triggers = None

        stamp = os.path.basename(os.path.dirname(url))
        # allow some skipped tests, but nothing else
        passed = exitcode in [0, 2]

        self.log_verbose('Fetched test result for %s/%s/%s %s (triggers: %s): %s' % (
            src, ver, arch, stamp, result_triggers, passed and 'pass' or 'fail'))

        # remove matching test requests, remember triggers
        satisfied_triggers = set()
        for request_map in [self.requested_tests, self.pending_tests]:
            for pending_ver, pending_archinfo in request_map.get(src, {}).copy().items():
                # don't consider newer requested versions
                if apt_pkg.version_compare(pending_ver, ver) > 0:
                    continue

                if result_triggers:
                    # explicitly recording/retrieving test triggers is the
                    # preferred (and robust) way of matching results to pending
                    # requests
                    for result_trigger in result_triggers:
                        satisfied_triggers.add(result_trigger)
                        try:
                            request_map[src][pending_ver][arch].remove(result_trigger)
                            self.log_verbose('-> matches pending request %s/%s/%s for trigger %s' %
                                             (src, pending_ver, arch, str(result_trigger)))
                        except (KeyError, ValueError):
                            self.log_verbose('-> does not match any pending request for %s/%s/%s' %
                                             (src, pending_ver, arch))
                else:
                    # ... but we still need to support results without
                    # testinfo.json and recorded triggers until we stop caring about
                    # existing wily and trusty results; match the latest result to all
                    # triggers for src that have at least the requested version
                    try:
                        t = pending_archinfo[arch]
                        self.log_verbose('-> matches pending request %s/%s for triggers %s' %
                                         (src, pending_ver, str(t)))
                        satisfied_triggers.update(t)
                        del request_map[src][pending_ver][arch]
                    except KeyError:
                        self.log_verbose('-> does not match any pending request for %s/%s' %
                                         (src, pending_ver))

        # FIXME: this is a hack that mostly applies to re-running tests
        # manually without giving a trigger. Tests which don't get
        # triggered by a particular kernel version are fine with that, so
        # add some heuristic once we drop the above code.
        if trigger:
            satisfied_triggers.add(trigger)

        # add this result
        src_arch_results = self.test_results.setdefault(src, {}).setdefault(arch, [stamp, {}, False])
        if passed:
            # update ever_passed field, unless we got triggered from
            # linux-meta*: we trigger separate per-kernel tests for reverse
            # test dependencies, and we don't want to track per-trigger
            # ever_passed. This would be wrong for everything except the
            # kernel, and the kernel team tracks per-kernel regressions already
            if not result_triggers or not result_triggers[0][0].startswith('linux-meta'):
                src_arch_results[2] = True
        if satisfied_triggers:
            for trig in satisfied_triggers:
                src_arch_results[1].setdefault(ver, {})[trig[0] + '/' + trig[1]] = passed
        else:
            # this result did not match any triggers? then we are in backwards
            # compat mode for results without recorded triggers; update all
            # results
            for trig in src_arch_results[1].setdefault(ver, {}):
                src_arch_results[1][ver][trig] = passed
        # update latest_stamp
        if stamp > src_arch_results[0]:
            src_arch_results[0] = stamp

    def failed_tests_for_trigger(self, trigsrc, trigver):
        '''Return (src, arch) set for failed tests for given trigger pkg'''

        result = set()
        trigger = trigsrc + '/' + trigver
        for src, srcinfo in self.test_results.items():
            for arch, (stamp, vermap, ever_passed) in srcinfo.items():
                for ver, trig_results in vermap.items():
                    if trig_results.get(trigger) is False:
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
            for arch in self.britney.options.adt_arches:
                for (testsrc, testver) in self.tests_for_source(src, ver, arch):
                    self.add_test_request(testsrc, testver, arch, src, ver)

        if self.britney.options.verbose:
            for src, verinfo in self.requested_tests.items():
                for ver, archinfo in verinfo.items():
                    for arch, triggers in archinfo.items():
                        self.log_verbose('Requesting %s/%s/%s autopkgtest to verify %s' %
                                         (src, ver, arch, ', '.join(['%s/%s' % i for i in triggers])))

    def submit(self):

        def _arches(verinfo):
            res = set()
            for archinfo in verinfo.values():
                res.update(archinfo.keys())
            return res

        def _trigsources(verinfo, arch):
            '''Calculate the triggers for a given verinfo map

            verinfo is ver -> arch -> {(triggering-src1, ver1), ...}, i. e. an
            entry of self.requested_tests[arch]

            Return {trigger1, ...}) set.
            '''
            triggers = set()
            for archinfo in verinfo.values():
                for (t, v) in archinfo.get(arch, []):
                    triggers.add(t + '/' + v)
            return triggers

        # build per-queue request strings for new test requests
        # TODO: Once we support version constraints in AMQP requests, add them
        # arch →  (queue_name, [(pkg, params), ...])
        arch_queues = {}
        for arch in self.britney.options.adt_arches:
            requests = []
            for pkg, verinfo in self.requested_tests.items():
                if arch in _arches(verinfo):
                    # if a package gets triggered by several sources, we can
                    # run just one test for all triggers; but for proposed
                    # kernels we want to run a separate test for each, so that
                    # the test runs under that particular kernel
                    triggers = _trigsources(verinfo, arch)
                    for t in sorted(triggers):
                        params = {'triggers': [t]}
                        requests.append((pkg, json.dumps(params)))
            arch_queues[arch] = ('debci-%s-%s' % (self.series, arch), requests)

        amqp_url = self.britney.options.adt_amqp

        if amqp_url.startswith('amqp://'):
            # in production mode, send them out via AMQP
            with kombu.Connection(amqp_url) as conn:
                for arch, (queue, requests) in arch_queues.items():
                    # don't use SimpleQueue here as it always declares queues;
                    # ACLs might not allow that
                    with kombu.Producer(conn, routing_key=queue, auto_declare=False) as p:
                        for (pkg, params) in requests:
                            p.publish(pkg + '\n' + params)
        elif amqp_url.startswith('file://'):
            # in testing mode, adt_amqp will be a file:// URL
            with open(amqp_url[7:], 'a') as f:
                for arch, (queue, requests) in arch_queues.items():
                    for (pkg, params) in requests:
                        f.write('%s:%s %s\n' % (queue, pkg, params))
        else:
            self.log_error('Unknown ADT_AMQP schema in %s' %
                           self.britney.options.adt_amqp)

        # mark them as pending now
        self.update_pending_tests()

    def collect_requested(self):
        '''Update results from swift for all requested packages

        This is normally redundant with collect(), but avoids actually
        sending test requests if results are already available. This mostly
        happens when you have to blow away results.cache and let it rebuild
        from scratch.
        '''
        for pkg, verinfo in copy.deepcopy(self.requested_tests).items():
            for archinfo in verinfo.values():
                for arch in archinfo:
                    self.fetch_swift_results(self.britney.options.adt_swift_url, pkg, arch)

    def collect(self, packages):
        '''Update results from swift for all pending packages

        Remove pending tests for which we have results.
        '''
        for pkg, verinfo in copy.deepcopy(self.pending_tests).items():
            for archinfo in verinfo.values():
                for arch in archinfo:
                    self.fetch_swift_results(self.britney.options.adt_swift_url, pkg, arch)
        # also update results for excuses whose tests failed, in case a
        # manual retry worked
        for (trigpkg, trigver) in packages:
            for (pkg, arch) in self.failed_tests_for_trigger(trigpkg, trigver):
                if arch not in self.pending_tests.get(trigpkg, {}).get(trigver, {}):
                    self.log_verbose('Checking for new results for failed %s on %s for trigger %s/%s' %
                                     (pkg, arch, trigpkg, trigver))
                    self.fetch_swift_results(self.britney.options.adt_swift_url, pkg, arch, (trigpkg, trigver))

        # update the results cache
        with open(self.results_cache_file + '.new', 'w') as f:
            json.dump(self.test_results, f, indent=2)
        os.rename(self.results_cache_file + '.new', self.results_cache_file)
        self.log_verbose('Updated results cache')

        # new results remove pending requests, update the on-disk cache
        self.update_pending_tests()

    def results(self, trigsrc, trigver):
        '''Return test results for triggering package

        Return (passed, src, ver, arch -> ALWAYSFAIL|PASS|FAIL|RUNNING|RUNNING-NEVERPASSED)
        iterable for all package tests that got triggered by trigsrc/trigver.
        '''
        # (src, ver) -> arch -> ALWAYSFAIL|PASS|FAIL|RUNNING|RUNNING-NEVERPASSED
        pkg_arch_result = {}
        trigger = trigsrc + '/' + trigver

        for arch in self.britney.options.adt_arches:
            for testsrc, testver in self.tests_for_source(trigsrc, trigver, arch):
                try:
                    (_, ver_map, ever_passed) = self.test_results[testsrc][arch]

                    # check if we have a result for any version of testsrc that
                    # was triggered for trigsrc/trigver; we prefer PASSes, as
                    # it could be that an unrelated package upload could break
                    # testsrc's tests at a later point
                    status = None
                    for ver, trigger_results in ver_map.items():
                        try:
                            status = trigger_results[trigger]
                            testver = ver
                            # if we found a PASS, we can stop searching
                            if status is True:
                                break
                        except KeyError:
                            pass

                    if status is None:
                        # no result? go to "still running" below
                        raise KeyError

                    if status:
                        result = 'PASS'
                    else:
                        # test failed, check ever_passed flag for that src/arch
                        # unless we got triggered from linux-meta*: we trigger
                        # separate per-kernel tests for reverse test
                        # dependencies, and we don't want to track per-trigger
                        # ever_passed. This would be wrong for everything
                        # except the kernel, and the kernel team tracks
                        # per-kernel regressions already
                        if ever_passed and not trigsrc.startswith('linux-meta') and trigsrc != 'linux':
                            result = 'REGRESSION'
                        else:
                            result = 'ALWAYSFAIL'
                except KeyError:
                    # no result for testsrc/testver/arch; still running?
                    try:
                        self.pending_tests[testsrc][testver][arch]
                        # if we can't find a result, assume that it has never passed (i.e. this is the first run)
                        (_, _, ever_passed) = self.test_results.get(testsrc, {}).get(arch, (None, None, False))

                        if ever_passed:
                            result = 'RUNNING'
                        else:
                            result = 'RUNNING-NEVERPASSED'
                    except KeyError:
                        # ignore if adt or swift results are disabled,
                        # otherwise this is unexpected
                        if not hasattr(self.britney.options, 'adt_swift_url'):
                            continue
                        # FIXME: Ignore this error for now as it crashes britney, but investigate!
                        self.log_error('FIXME: Result for %s/%s/%s (triggered by %s) is neither known nor pending!' %
                                       (testsrc, testver, arch, trigger))
                        continue

                pkg_arch_result.setdefault((testsrc, testver), {})[arch] = result

        for ((testsrc, testver), arch_results) in pkg_arch_result.items():
            r = arch_results.values()
            passed = 'REGRESSION' not in r and 'RUNNING' not in r
            yield (passed, testsrc, testver, arch_results)
