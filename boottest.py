# -*- coding: utf-8 -*-

# Copyright (C) 2015 Canonical Ltd.

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
import urllib

import apt_pkg

from consts import BINARIES


FETCH_RETRIES = 3


class TouchManifest(object):
    """Parses a corresponding touch image manifest.

    Based on http://cdimage.u.c/ubuntu-touch/daily-preinstalled/pending/vivid-preinstalled-touch-armhf.manifest

    Assumes the deployment is arranged in a way the manifest is available
    and fresh on:

    '{britney_cwd}/boottest/images/{distribution}/{series}/manifest'

    Only binary name matters, version is ignored, so callsites can:

    >>> manifest = TouchManifest('ubuntu-touch', 'vivid')
    >>> 'webbrowser-app' in manifest
    True
    >>> 'firefox' in manifest
    False

    """

    def __init__(self, project, series, verbose=False, fetch=True):
        self.verbose = verbose
        self.path = "boottest/images/{}/{}/manifest".format(
            project, series)
        success = False
        if fetch:
            retries = FETCH_RETRIES
            success = self.__fetch_manifest(project, series)

            while retries > 0 and not success:
                success = self.__fetch_manifest(project, series)
                retries -= 1
        if not success:
            print("E: [%s] - Unable to fetch manifest: %s %s" % (
                time.asctime(), project, series))

        self._manifest = self._load()

    def __fetch_manifest(self, project, series):
        url = "http://cdimage.ubuntu.com/{}/daily-preinstalled/" \
              "pending/{}-preinstalled-touch-armhf.manifest".format(
                  project, series
        )
        success = False
        if self.verbose:
            print(
                "I: [%s] - Fetching manifest from %s" % (
                    time.asctime(), url))
            print("I: [%s] - saving it to %s" % (time.asctime(), self.path))
        response = urllib.urlopen(url)
        # Only [re]create the manifest file if one was successfully downloaded
        # this allows for an existing image to be used if the download fails.
        if response.code == 200:
            path_dir = os.path.dirname(self.path)
            if not os.path.exists(path_dir):
                os.makedirs(path_dir)
            with open(self.path, 'w') as fp:
                fp.write(response.read())
            success = True

        return success

    def _load(self):
        pkg_list = []

        if not os.path.exists(self.path):
            return pkg_list

        with open(self.path) as fd:
            for line in fd.readlines():
                # skip headers and metadata
                if 'DOCTYPE' in line:
                    continue
                name, version = line.split()
                name = name.split(':')[0]
                if name == 'click':
                    continue
                pkg_list.append(name)

        return sorted(pkg_list)

    def __contains__(self, key):
        return key in self._manifest


class BootTest(object):
    """Boottest criteria for Britney.

    This class provides an API for handling the boottest-jenkins
    integration layer (mostly derived from auto-package-testing/adt):
    """
    VALID_STATUSES = ('PASS', 'SKIPPED')

    EXCUSE_LABELS = {
        "PASS": '<span style="background:#87d96c">Pass</span>',
        "SKIPPED": '<span style="background:#e5c545">Skipped</span>',
        "FAIL": '<span style="background:#ff6666">Regression</span>',
        "RUNNING": '<span style="background:#99ddff">Test in progress</span>',
    }

    script_path = os.path.expanduser(
        "~/auto-package-testing/jenkins/boottest-britney")

    def __init__(self, britney, distribution, series, debug=False):
        self.britney = britney
        self.distribution = distribution
        self.series = series
        self.debug = debug
        self.rc_path = None
        self._read()
        manifest_fetch = getattr(
            self.britney.options, "boottest_fetch", "no") == "yes"
        self.phone_manifest = TouchManifest(
            'ubuntu-touch', self.series, fetch=manifest_fetch,
            verbose=self.britney.options.verbose)

    @property
    def _request_path(self):
        return "boottest/work/adt.request.%s" % self.series

    @property
    def _result_path(self):
        return "boottest/work/adt.result.%s" % self.series

    def _ensure_rc_file(self):
        if self.rc_path:
            return
        self.rc_path = os.path.abspath("boottest/rc.%s" % self.series)
        with open(self.rc_path, "w") as rc_file:
            home = os.path.expanduser("~")
            print(dedent("""\
                release: %s
                aptroot: ~/.chdist/%s-proposed-armhf/
                apturi: file:%s/mirror/%s
                components: main restricted universe multiverse
                rsync_host: rsync://tachash.ubuntu-ci/boottest/
                datadir: ~/proposed-migration/boottest/data""" %
                         (self.series, self.series, home, self.distribution)),
                         file=rc_file)

    def _run(self, *args):
        self._ensure_rc_file()
        if not os.path.exists(self.script_path):
            print("E: [%s] - Boottest/Jenking glue script missing: %s" % (
                time.asctime(), self.script_path))
            return '-'
        command = [
            self.script_path,
            "-c", self.rc_path,
            "-r", self.series,
            "-PU",
            ]
        if self.debug:
            command.append("-d")
        command.extend(args)
        return subprocess.check_output(command).strip()

    def _read(self):
        """Loads a list of results (sources tests and their status).

        Provides internal data for `get_status()`.
        """
        self.pkglist = defaultdict(dict)
        if not os.path.exists(self._result_path):
            return
        with open(self._result_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("Suite:") or line.startswith("Date:"):
                    continue
                linebits = line.split()
                if len(linebits) < 2:
                    print("W: Invalid line format: '%s', skipped" % line)
                    continue
                (src, ver, status) = linebits[:3]
                if not (src in self.pkglist and ver in self.pkglist[src]):
                    self.pkglist[src][ver] = status

    def get_status(self, name, version):
        """Return test status for the given source name and version."""
        return self.pkglist[name][version]

    def request(self, packages):
        """Requests boottests for the given sources list ([(src, ver),])."""
        request_path = self._request_path
        if os.path.exists(request_path):
            os.unlink(request_path)
        with closing(tempfile.NamedTemporaryFile(mode="w")) as request_file:
            for src, ver in packages:
                if src in self.pkglist and ver in self.pkglist[src]:
                    continue
                print("%s %s" % (src, ver), file=request_file)
                # Update 'pkglist' so even if submit/collect is not called
                # (dry-run), britney has some results.
                self.pkglist[src][ver] = 'RUNNING'
            request_file.flush()
            self._run("request", "-O", request_path, request_file.name)

    def submit(self):
        """Submits the current boottests requests for processing."""
        self._run("submit", self._request_path)

    def collect(self):
        """Collects boottests results and updates internal registry."""
        self._run("collect", "-O", self._result_path)
        self._read()
        if not self.britney.options.verbose:
            return
        for src in sorted(self.pkglist):
            for ver in sorted(self.pkglist[src], cmp=apt_pkg.version_compare):
                status = self.pkglist[src][ver]
                print("I: [%s] - Collected boottest status for %s_%s: "
                      "%s" % (time.asctime(), src, ver, status))

    def needs_test(self, name, version):
        """Whether or not the given source and version should be tested.

        Sources are only considered for boottesting if they produce binaries
        that are part of the phone image manifest. See `TouchManifest`.
        """
        # Discover all binaries for the 'excused' source.
        unstable_sources = self.britney.sources['unstable']
        # Dismiss if source is not yet recognized (??).
        if name not in unstable_sources:
            return False
        # Binaries are a seq of "<binname>/<arch>" and, practically, boottest
        # is only concerned about armhf binaries mentioned in the phone
        # manifest. Anything else should be skipped.
        phone_binaries = [
            b for b in unstable_sources[name][BINARIES]
            if b.split('/')[1] in self.britney.options.boottest_arches.split()
            and b.split('/')[0] in self.phone_manifest
        ]
        return bool(phone_binaries)
