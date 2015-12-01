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
import re
import urllib.parse
from urllib.request import urlopen

import apt_pkg
import amqplib.client_0_8 as amqp

from consts import (AUTOPKGTEST, BINARIES, DEPENDS, RDEPENDS, SOURCE, VERSION)


def srchash(src):
    '''archive hash prefix for source package'''

    if src.startswith('lib'):
        return src[:4]
    else:
        return src[0]


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
        # trigger -> src -> [arch]
        self.requested_tests = {}
        # same map for tests requested in previous runs
        self.pending_tests = None
        self.pending_tests_file = os.path.join(self.test_state_dir, 'pending.json')

        if not os.path.isdir(self.test_state_dir):
            os.mkdir(self.test_state_dir)
        self.read_pending_tests()

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

        Initialize self.pending_tests with that data.
        '''
        assert self.pending_tests is None, 'already initialized'
        if not os.path.exists(self.pending_tests_file):
            self.log_verbose('No %s, starting with no pending tests' %
                             self.pending_tests_file)
            self.pending_tests = {}
            return
        with open(self.pending_tests_file) as f:
            self.pending_tests = json.load(f)
        self.log_verbose('Read pending requested tests from %s: %s' %
                         (self.pending_tests_file, self.pending_tests))

    def update_pending_tests(self):
        '''Update pending tests after submitting requested tests'''

        # merge requested_tests into pending_tests
        for trigger, srcmap in self.requested_tests.items():
            for src, archlist in srcmap.items():
                try:
                    arches = set(self.pending_tests[trigger][src])
                except KeyError:
                    arches = set()
                arches.update(archlist)
                self.pending_tests.setdefault(trigger, {})[src] = sorted(arches)
        self.requested_tests.clear()

        # write it
        with open(self.pending_tests_file + '.new', 'w') as f:
            json.dump(self.pending_tests, f, indent=2)
        os.rename(self.pending_tests_file + '.new', self.pending_tests_file)
        self.log_verbose('Updated pending requested tests in %s' %
                         self.pending_tests_file)

    def add_test_request(self, src, arch, trigger):
        '''Add one test request to the local self.requested_tests queue

        trigger is "pkgname/version" of the package that triggers the testing
        of src.

        This will only be done if that test wasn't already requested in a
        previous run (i. e. not already in self.pending_tests) or there already
        is a result for it.
        '''
        # Don't re-request if we already have a result
        try:
            self.test_results[trigger][src][arch]
            self.log_verbose('There already is a result for %s/%s triggered by %s' %
                             (src, arch, trigger))
            return
        except KeyError:
            pass

        # Don't re-request if it's already pending
        if arch in self.pending_tests.get(trigger, {}).get(src, []):
            self.log_verbose('test %s/%s for %s is already pending, not queueing' %
                             (src, arch, trigger))
        else:
            arch_list = self.requested_tests.setdefault(trigger, {}).setdefault(src, [])
            assert arch not in arch_list
            arch_list.append(arch)

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

        # prepare query: get all runs with a timestamp later than the latest
        # run_id for this package/arch; '@' is at the end of each run id, to
        # mark the end of a test run directory path
        # example: <autopkgtest-wily>wily/amd64/libp/libpng/20150630_054517@/result.tar
        query = {'delimiter': '@',
                 'prefix': '%s/%s/%s/%s/' % (self.series, arch, srchash(src), src)}

        # determine latest run_id from results
        latest_run_id = self.latest_run_for_package(src, arch)
        if latest_run_id:
            query['marker'] = query['prefix'] + latest_run_id

        # request new results from swift
        url = os.path.join(swift_url, 'autopkgtest-' + self.series)
        url += '?' + urllib.parse.urlencode(query)
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
                os.path.join(swift_url, 'autopkgtest-' + self.series, p, 'result.tar'), src, arch)

    def fetch_one_result(self, url, src, arch):
        '''Download one result URL for source/arch

        Remove matching pending_tests entries.
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
                testinfo = json.loads(tar.extractfile('testinfo.json').read().decode())
        except (KeyError, ValueError, tarfile.TarError) as e:
            self.log_error('%s is damaged, ignoring: %s' % (url, str(e)))
            # ignore this; this will leave an orphaned request in pending.json
            # and thus require manual retries after fixing the tmpfail, but we
            # can't just blindly attribute it to some pending test.
            return

        if src != ressrc:
            self.log_error('%s is a result for package %s, but expected package %s' %
                           (url, ressrc, src))
            return

        # parse recorded triggers in test result
        for e in testinfo.get('custom_environment', []):
            if e.startswith('ADT_TEST_TRIGGERS='):
                result_triggers = [i for i in e.split('=', 1)[1].split() if '/' in i]
                break
        else:
            self.log_error('%s result has no ADT_TEST_TRIGGERS, ignoring')
            return

        stamp = os.path.basename(os.path.dirname(url))
        # allow some skipped tests, but nothing else
        passed = exitcode in [0, 2]

        self.log_verbose('Fetched test result for %s/%s/%s %s (triggers: %s): %s' % (
            src, ver, arch, stamp, result_triggers, passed and 'pass' or 'fail'))

        # remove matching test requests
        for trigger in result_triggers:
            for request_map in [self.requested_tests, self.pending_tests]:
                try:
                    arch_list = request_map[trigger][src]
                    arch_list.remove(arch)
                    if not arch_list:
                        del request_map[trigger][src]
                    if not request_map[trigger]:
                        del request_map[trigger]
                    self.log_verbose('-> matches pending request %s/%s for trigger %s' % (src, arch, trigger))
                except (KeyError, ValueError):
                    self.log_verbose('-> does not match any pending request for %s/%s' % (src, arch))

        # add this result
        for trigger in result_triggers:
            # If a test runs because of its own package (newer version), ensure
            # that we got a new enough version; FIXME: this should be done more
            # generically by matching against testpkg-versions
            (trigsrc, trigver) = trigger.split('/', 1)
            if trigsrc == src and apt_pkg.version_compare(ver, trigver) < 0:
                self.log_error('test trigger %s, but run for older version %s, ignoring' % (trigger, ver))
                continue

            result = self.test_results.setdefault(trigger, {}).setdefault(
                src, {}).setdefault(arch, [False, None, ''])

            # don't clobber existing passed results with failures from re-runs
            result[0] = result[0] or passed
            result[1] = ver
            if stamp > result[2]:
                result[2] = stamp

    def failed_tests_for_trigger(self, trigger):
        '''Return (src, arch) set for failed tests for given trigger'''

        failed = set()
        for src, srcinfo in self.test_results.get(trigger, {}).items():
            for arch, result in srcinfo.items():
                if not result[0]:
                    failed.add((src, arch))
        return failed

    def check_ever_passed(self, src, arch):
        '''Check if tests for src ever passed on arch'''

        # FIXME: add caching
        for srcmap in self.test_results.values():
            try:
                if srcmap[src][arch][0]:
                    return True
            except KeyError:
                pass
        return False

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
                    self.add_test_request(testsrc, arch, src + '/' + ver)

        if self.britney.options.verbose:
            for trigger, srcmap in self.requested_tests.items():
                for src, archlist in srcmap.items():
                    self.log_verbose('Requesting %s autopkgtest on %s to verify %s' %
                                     (src, ' '.join(archlist), trigger))

    def submit(self):
        # build per-queue request strings for new test requests
        # TODO: Once we support version constraints in AMQP requests, add them
        # queue_name -> [(pkg, params), ...])
        queues = {}
        for trigger, srcmap in self.requested_tests.items():
            params = {'triggers': [trigger]}
            if self.britney.options.adt_ppas:
                params['ppas'] = self.britney.options.adt_ppas
            for src, archlist in srcmap.items():
                for arch in archlist:
                    qname = 'debci-%s-%s' % (self.series, arch)
                    queues.setdefault(qname, []).append((src, json.dumps(params)))

        amqp_url = self.britney.options.adt_amqp

        if amqp_url.startswith('amqp://'):
            # in production mode, send them out via AMQP
            creds = urllib.parse.urlsplit(amqp_url, allow_fragments=False)
            with amqp.Connection(creds.hostname, userid=creds.username,
                                 password=creds.password) as amqp_con:
                with amqp_con.channel() as ch:
                    for queue, requests in queues.items():
                        for (pkg, params) in requests:
                            ch.basic_publish(amqp.Message(pkg + '\n' + params),
                                             routing_key=queue)
        elif amqp_url.startswith('file://'):
            # in testing mode, adt_amqp will be a file:// URL
            with open(amqp_url[7:], 'a') as f:
                for queue, requests in queues.items():
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
        # build src -> arch -> triggers inverted map
        requests_by_src = {}
        for trigger, srcmap in self.requested_tests.items():
            for src, archlist in srcmap.items():
                for arch in archlist:
                    requests_by_src.setdefault(src, {}).setdefault(arch, set()).add(trigger)

        for src, archmap in requests_by_src.items():
            for arch, triggers in archmap.items():
                self.fetch_swift_results(self.britney.options.adt_swift_url, src, arch)

    def collect(self, packages):
        '''Update results from swift for all pending packages

        Remove pending tests for which we have results.
        '''
        # build src -> arch -> triggers inverted map
        requests_by_src = {}
        for trigger, srcmap in self.pending_tests.items():
            for src, archlist in srcmap.items():
                for arch in archlist:
                    requests_by_src.setdefault(src, {}).setdefault(arch, set()).add(trigger)

        for src, archmap in requests_by_src.items():
            for arch, triggers in archmap.items():
                self.fetch_swift_results(self.britney.options.adt_swift_url, src, arch)

        # also update results for excuses whose tests failed, in case a
        # manual retry worked
        for (trigpkg, trigver) in packages:
            trigger = trigpkg + '/' + trigver
            for (src, arch) in self.failed_tests_for_trigger(trigger):
                if arch not in self.pending_tests.get(trigger, {}).get(src, []):
                    self.log_verbose('Checking for new results for failed %s on %s for trigger %s' %
                                     (src, arch, trigger))
                    self.fetch_swift_results(self.britney.options.adt_swift_url, src, arch)

        # update the results cache
        with open(self.results_cache_file + '.new', 'w') as f:
            json.dump(self.test_results, f, indent=2)
        os.rename(self.results_cache_file + '.new', self.results_cache_file)
        self.log_verbose('Updated results cache')

        # new results remove pending requests, update the on-disk cache
        self.update_pending_tests()

    def results(self, trigsrc, trigver):
        '''Return test results for triggering package

        Return (passed, src, ver, arch -> ALWAYSFAIL|PASS|REGRESSION|RUNNING|RUNNING-ALWAYSFAIL)
        iterable for all package tests that got triggered by trigsrc/trigver.
        '''
        # (src, ver) -> arch -> ALWAYSFAIL|PASS|REGRESSION|RUNNING|RUNNING-ALWAYSFAIL
        pkg_arch_result = {}
        trigger = trigsrc + '/' + trigver

        for arch in self.britney.options.adt_arches:
            for testsrc, testver in self.tests_for_source(trigsrc, trigver, arch):
                ever_passed = self.check_ever_passed(testsrc, arch)

                # Do we have a result already? (possibly for an older or newer
                # version, that's okay)
                try:
                    r = self.test_results[trigger][testsrc][arch]
                    testver = r[1]
                    if r[0]:
                        result = 'PASS'
                    else:
                        # Special-case triggers from linux-meta*: we cannot compare
                        # results against different kernels, as e. g. a DKMS module
                        # might work against the default kernel but fail against a
                        # different flavor; so for those, ignore the "ever
                        # passed" check; FIXME: check against trigsrc only
                        if trigsrc.startswith('linux-meta') or trigsrc == 'linux':
                            ever_passed = False

                        result = ever_passed and 'REGRESSION' or 'ALWAYSFAIL'
                except KeyError:
                    # no result for testsrc/arch; still running?
                    if arch in self.pending_tests.get(trigger, {}).get(testsrc, []):
                        result = ever_passed and 'RUNNING' or 'RUNNING-ALWAYSFAIL'
                    else:
                        # ignore if adt or swift results are disabled,
                        # otherwise this is unexpected
                        if not hasattr(self.britney.options, 'adt_swift_url'):
                            continue
                        raise RuntimeError('Result for %s/%s/%s (triggered by %s) is neither known nor pending!' %
                                           (testsrc, testver, arch, trigger))

                pkg_arch_result.setdefault((testsrc, testver), {})[arch] = result

        for ((testsrc, testver), arch_results) in pkg_arch_result.items():
            r = arch_results.values()
            passed = 'REGRESSION' not in r and 'RUNNING' not in r
            yield (passed, testsrc, testver, arch_results)
