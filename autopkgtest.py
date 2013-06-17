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
import logging
import os
import subprocess
import tempfile
from textwrap import dedent
import time


adt_britney = os.path.expanduser("~/auto-package-testing/jenkins/adt-britney")


class AutoPackageTest(object):
    """autopkgtest integration

    Look for autopkgtest jobs to run for each update that is otherwise a
    valid candidate, and collect the results.  If an update causes any
    autopkgtest jobs to be run, then they must all pass before the update is
    accepted.
    """

    def __init__(self, britney, series, debug=False):
        self.britney = britney
        self.series = series
        self.debug = debug
        self.read()
        self.rc_path = None

    def _ensure_rc_file(self):
        if self.rc_path:
            return
        self.rc_path = os.path.expanduser(
            "~/proposed-migration/autopkgtest/rc.%s" % self.series)
        with open(self.rc_path, "w") as rc_file:
            home = os.path.expanduser("~")
            print(dedent("""\
                aptroot: ~/.chdist/%s-proposed-amd64/
                apturi: file:%s/mirror/ubuntu
                components: main restricted universe multiverse
                rsync_host: rsync://10.189.74.2/adt/
                datadir: ~/proposed-migration/autopkgtest/data""" %
                (self.series, home)), file=rc_file)

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
                        logging.warning(
                            "Invalid line format: '%s', skipped" % line)
                        continue
                    yield linebits

    def read(self):
        self.pkglist = defaultdict(dict)
        self.pkgcauses = defaultdict(lambda: defaultdict(list))
        for linebits in self._parse(self._result_path):
            src = linebits.pop(0)
            ver = linebits.pop(0)
            self.pkglist[src][ver] = {
                "status": "NEW",
                "causes": {},
                }
            try:
                status = linebits.pop(0).upper()
                self.pkglist[src][ver]["status"] = status
                while True:
                    trigsrc = linebits.pop(0)
                    trigver = linebits.pop(0)
                    self.pkglist[src][ver]["causes"][trigsrc] = trigver
                    self.pkgcauses[trigsrc][trigver].append((status, src, ver))
            except IndexError:
                # End of the list
                pass

    def _adt_britney(self, *args):
        command = [
            adt_britney,
            "-c", self.rc_path, "-r", self.series, "-a", "amd64", "-PU",
            ]
        if self.debug:
            command.append("-d")
        command.extend(args)
        subprocess.check_call(command)

    def request(self, packages):
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
        for linebits in self._parse(request_path):
            # Make sure that there's an entry in pkgcauses for each new
            # request, so that results() gives useful information without
            # relying on the submit/collect cycle.  This improves behaviour
            # in dry-run mode.
            src = linebits.pop(0)
            ver = linebits.pop(0)
            if self.britney.options.verbose:
                print("I: [%s] - Requested autopkgtest for %s_%s" %
                      (time.asctime(), src, ver))
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
        self._ensure_rc_file()
        request_path = self._request_path
        if os.path.exists(request_path):
            self._adt_britney("submit", request_path)

    def collect(self):
        self._ensure_rc_file()
        result_path = self._result_path
        self._adt_britney("collect", "-O", result_path)
        self.read()

    def results(self, trigsrc, trigver):
        for status, src, ver in self.pkgcauses[trigsrc][trigver]:
            yield status, src, ver
