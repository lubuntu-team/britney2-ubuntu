# -*- coding: utf-8 -*-

# Copyright (C) 2013 Canonical Ltd.
# Author: Colin Watson <cjwatson@ubuntu.com>
# Partly based on code in auto-package-testing by
# Jean-Baptiste Lallement <jean-baptiste.lallement@canonical.com>

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

from __future__ import print_function

from collections import defaultdict
from contextlib import closing
import os
import subprocess
import tempfile
from textwrap import dedent
import time
import apt_pkg

import kombu

from consts import (AUTOPKGTEST, BINARIES, RDEPENDS, SOURCE)


adt_britney = os.path.expanduser("~/auto-package-testing/jenkins/adt-britney")

ADT_PASS = ["PASS", "ALWAYSFAIL"]
ADT_EXCUSES_LABELS = {
    "PASS": '<span style="background:#87d96c">Pass</span>',
    "ALWAYSFAIL": '<span style="background:#e5c545">Always failed</span>',
    "REGRESSION": '<span style="background:#ff6666">Regression</span>',
    "RUNNING": '<span style="background:#99ddff">Test in progress</span>',
}


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
        self.read()
        self.rc_path = None  # for adt-britney, obsolete
        self.test_state_dir = os.path.join(britney.options.unstable,
                                           'autopkgtest')
        # map of requested tests from request()
        # src -> ver -> {(triggering-src1, ver1), ...}
        self.requested_tests = {}
        # same map for tests requested in previous runs
        self.pending_tests = None
        self.pending_tests_file = os.path.join(self.test_state_dir, 'pending.txt')

        if not os.path.isdir(self.test_state_dir):
            os.mkdir(self.test_state_dir)
        self.read_pending_tests()

    def log_verbose(self, msg):
        if self.britney.options.verbose:
            print('I: [%s] - %s' % (time.asctime(), msg))

    def log_error(self, msg):
        print('E: [%s] - %s' % (time.asctime(), msg))

    def tests_for_source(self, src, ver):
        '''Iterate over all tests that should be run for given source'''

        sources_info = self.britney.sources['unstable']
        # FIXME: For now assume that amd64 has all binaries that we are
        # interested in for reverse dependency checking
        binaries_info = self.britney.binaries['unstable']['amd64'][0]

        srcinfo = sources_info[src]
        # we want to test the package itself, if it still has a test in
        # unstable
        if srcinfo[AUTOPKGTEST]:
            yield (src, ver)

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
                if sources_info[rdep_src][AUTOPKGTEST]:
                    # we don't care about the version of rdep
                    yield (rdep_src, None)

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
                    (src, ver, trigsrc, trigver) = l.split()
                except ValueError:
                    self.log_error('ignoring malformed line in %s: %s' %
                                   (self.pending_tests_file, l))
                    continue
                if ver == '-':
                    ver = None
                if trigver == '-':
                    trigver = None
                self.pending_tests.setdefault(src, {}).setdefault(
                    ver, set()).add((trigsrc, trigver))
        self.log_verbose('Read pending requested tests from %s: %s' %
                         (self.pending_tests_file, self.pending_tests))

    def update_pending_tests(self):
        '''Update pending tests after submitting requested tests

        Update UNSTABLE/autopkgtest/requested.txt, see read_pending_tests() for
        the format.
        '''
        # merge requested_tests into pending_tests
        for src, verinfo in self.requested_tests.items():
            for ver, triggers in verinfo.items():
                self.pending_tests.setdefault(src, {}).setdefault(
                    ver, set()).update(triggers)
        self.requested_tests = {}

        # write it
        with open(self.pending_tests_file + '.new', 'w') as f:
            for src in sorted(self.pending_tests):
                for ver in sorted(self.pending_tests[src]):
                    for (trigsrc, trigver) in sorted(self.pending_tests[src][ver]):
                        if ver is None:
                            ver = '-'
                        if trigver is None:
                            trigver = '-'
                        f.write('%s %s %s %s\n' % (src, ver, trigsrc, trigver))
        os.rename(self.pending_tests_file + '.new', self.pending_tests_file)
        self.log_verbose('Updated pending requested tests in %s' %
                         self.pending_tests_file)

    def add_test_request(self, src, ver, trigsrc, trigver):
        '''Add one test request to the local self.requested_tests queue

        This will only be done if that test wasn't already requested in a
        previous run, i. e. it is already in self.pending_tests.

        versions can be None if you don't care about the particular version.
        '''
        if (trigsrc, trigver) in self.pending_tests.get(src, {}).get(ver, set()):
            self.log_verbose('test %s/%s for %s/%s is already pending, not queueing' %
                             (src, ver, trigsrc, trigver))
            return
        self.requested_tests.setdefault(src, {}).setdefault(
            ver, set()).add((trigsrc, trigver))

    #
    # obsolete adt-britney helpers
    #

    def _ensure_rc_file(self):
        if self.rc_path:
            return
        self.rc_path = os.path.expanduser(
            "~/proposed-migration/autopkgtest/rc.%s" % self.series)
        with open(self.rc_path, "w") as rc_file:
            home = os.path.expanduser("~")
            print(dedent("""\
                release: %s
                aptroot: ~/.chdist/%s-proposed-amd64/
                apturi: file:%s/mirror/%s
                components: main restricted universe multiverse
                rsync_host: rsync://tachash.ubuntu-ci/adt/
                datadir: ~/proposed-migration/autopkgtest/data""" %
                         (self.series, self.series, home, self.distribution)),
                         file=rc_file)

    @property
    def _request_path(self):
        return os.path.expanduser(
            "~/proposed-migration/autopkgtest/work/adt.request.%s" %
            self.series)

    @property
    def _result_path(self):
        return os.path.expanduser(
            "~/proposed-migration/autopkgtest/work/adt.result.%s" %
            self.series)

    def _parse(self, path):
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("Suite:") or line.startswith("Date:"):
                        continue
                    linebits = line.split()
                    if len(linebits) < 2:
                        print("W: Invalid line format: '%s', skipped" % line)
                        continue
                    yield linebits

    def read(self):
        '''Loads a list of results

        This function loads a list of results returned by __parse() and builds
        2 lists:
            - a list of source package/version with all the causes that
            triggered a test and the result of the test for this trigger.
            - a list of packages/version that triggered a test with the source
            package/version and result triggered by this package.
        These lists will be used in result() called from britney.py to generate
        excuses and now which uploads passed, caused regression or which tests
        have always been failing
        '''
        self.pkglist = defaultdict(dict)
        self.pkgcauses = defaultdict(lambda: defaultdict(list))
        for linebits in self._parse(self._result_path):
            (src, ver, status) = linebits[:3]

            if not (src in self.pkglist and ver in self.pkglist[src]):
                self.pkglist[src][ver] = {
                    "status": status,
                    "causes": {}
                }

            i = iter(linebits[3:])
            for trigsrc, trigver in zip(i, i):
                self.pkglist[src][ver]['causes'].setdefault(
                    trigsrc, []).append((trigver, status))
                self.pkgcauses[trigsrc][trigver].append((status, src, ver))

    def _adt_britney(self, *args):
        command = [
            adt_britney,
            "-c", self.rc_path, "-r", self.series, "-PU",
            ]
        if self.debug:
            command.append("-d")
        command.extend(args)
        subprocess.check_call(command)

    #
    # Public API
    #

    def request(self, packages, excludes=None):
        if excludes is None:
            excludes = []

        self.log_verbose('Requested autopkgtests for %s, exclusions: %s' %
                         (['%s/%s' % i for i in packages], str(excludes)))
        for src, ver in packages:
            for (testsrc, testver) in self.tests_for_source(src, ver):
                if testsrc not in excludes:
                    self.add_test_request(testsrc, testver, src, ver)

        if self.britney.options.verbose:
            for src, verinfo in self.requested_tests.items():
                for ver, triggers in verinfo.items():
                    self.log_verbose('Requesting %s/%s autopkgtest to verify %s' %
                                     (src, ver, ', '.join(['%s/%s' % i for i in triggers])))

        # deprecated requests for old Jenkins/lp:auto-package-testing, will go
        # away

        self._ensure_rc_file()
        request_path = self._request_path
        if os.path.exists(request_path):
            os.unlink(request_path)
        with closing(tempfile.NamedTemporaryFile(mode="w")) as request_file:
            for src, ver in packages:
                if src in self.pkglist and ver in self.pkglist[src]:
                    continue
                print("%s %s" % (src, ver), file=request_file)
            request_file.flush()
            self._adt_britney("request", "-O", request_path, request_file.name)

        # Remove packages that have been identified as invalid candidates for
        # testing from the request file i.e run_autopkgtest = False
        with open(request_path, 'r') as request_file:
            lines = request_file.readlines()
        with open(request_path, 'w') as request_file:
            for line in lines:
                src = line.split()[0]
                if src not in excludes:
                    request_file.write(line)
                else:
                    if self.britney.options.verbose:
                        self.log_verbose("Requested autopkgtest for %s but "
                                         "run_autopkgtest set to False" % src)

        for linebits in self._parse(request_path):
            # Make sure that there's an entry in pkgcauses for each new
            # request, so that results() gives useful information without
            # relying on the submit/collect cycle.  This improves behaviour
            # in dry-run mode.
            src = linebits.pop(0)
            ver = linebits.pop(0)
            if self.britney.options.verbose:
                self.log_verbose("Requested autopkgtest for %s_%s (%s)" %
                                 (src, ver, " ".join(linebits)))
            try:
                status = linebits.pop(0).upper()
                while True:
                    trigsrc = linebits.pop(0)
                    trigver = linebits.pop(0)
                    for status, csrc, cver in self.pkgcauses[trigsrc][trigver]:
                        if csrc == trigsrc and cver == trigver:
                            break
                    else:
                        self.pkgcauses[trigsrc][trigver].append(
                            (status, src, ver))
            except IndexError:
                # End of the list
                pass

    def submit(self):
        # send AMQP requests for new test requests
        # TODO: Once we support version constraints in AMQP requests, add them
        queues = ['debci-%s-%s' % (self.series, arch)
                  for arch in self.britney.options.adt_arches.split()]

        try:
            amqp_url = self.britney.options.adt_amqp
        except AttributeError:
            self.log_error('ADT_AMQP not set, cannot submit requests')
            return

        if amqp_url.startswith('amqp://'):
            with kombu.Connection(amqp_url) as conn:
                for q in queues:
                    # don't use SimpleQueue here as it always declares queues;
                    # ACLs might not allow that
                    with kombu.Producer(conn, routing_key=q, auto_declare=False) as p:
                        for pkg in self.requested_tests:
                            p.publish(pkg)
        elif amqp_url.startswith('file://'):
            # in testing mode, adt_amqp will be a file:// URL
            with open(amqp_url[7:], 'a') as f:
                for pkg in self.requested_tests:
                    for q in queues:
                        f.write('%s:%s\n' % (q, pkg))
        else:
            self.log_error('Unknown ADT_AMQP schema in %s' %
                           self.britney.options.adt_amqp)

        # mark them as pending now
        self.update_pending_tests()

        # deprecated requests for old Jenkins/lp:auto-package-testing, will go
        # away
        self._ensure_rc_file()
        request_path = self._request_path
        if os.path.exists(request_path):
            self._adt_britney("submit", request_path)

    def collect(self):
        self._ensure_rc_file()
        result_path = self._result_path
        self._adt_britney("collect", "-O", result_path)
        self.read()
        if self.britney.options.verbose:
            for src in sorted(self.pkglist):
                for ver in sorted(self.pkglist[src],
                                  cmp=apt_pkg.version_compare):
                    for trigsrc in sorted(self.pkglist[src][ver]['causes']):
                        for trigver, status \
                                in self.pkglist[src][ver]['causes'][trigsrc]:
                            self.log_verbose("Collected autopkgtest status "
                                             "for %s_%s/%s_%s: " "%s" %
                                             (src, ver, trigsrc, trigver, status))

    def results(self, trigsrc, trigver):
        for status, src, ver in self.pkgcauses[trigsrc][trigver]:
            # Check for regression
            if status == 'FAIL':
                passed_once = False
                for lver in self.pkglist[src]:
                    for trigsrc in self.pkglist[src][lver]['causes']:
                        for trigver, status \
                                in self.pkglist[src][lver]['causes'][trigsrc]:
                            if status == 'PASS':
                                passed_once = True
                if not passed_once:
                    status = 'ALWAYSFAIL'
                else:
                    status = 'REGRESSION'
            yield status, src, ver
