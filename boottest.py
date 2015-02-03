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

import os
import subprocess
import time
import urllib

from consts import BINARIES


class TouchManifest(object):
    """Parses a corresponding touch image manifest.

    Based on http://cdimage.u.c/ubuntu-touch/daily-preinstalled/pending/vivid-preinstalled-touch-armhf.manifest

    Assumes the deployment is arranged in a way the manifest is available
    and fresh on:

    '{britney_cwd}/boottest/images/{distribution}/{series}/manifest'

    Only binary name matters, version is ignored, so callsites can:

    >>> manifest = TouchManifest('ubuntu', 'vivid')
    >>> 'webbrowser-app' in manifest
    True
    >>> 'firefox' in manifest
    False

    """

    def __fetch_manifest(self, distribution, series):
        url = "http://cdimage.ubuntu.com/{}/daily-preinstalled/" \
              "pending/{}-preinstalled-touch-armhf.manifest".format(
                  distribution, series
        )
        print("I: [%s] - Fetching manifest from %s" % (time.asctime(), url))
        response = urllib.urlopen(url)
        # Only [re]create the manifest file if one was successfully downloaded
        # this allows for an existing image to be used if the download fails.
        if response.code == 200:
            os.makedirs(os.path.dirname(self.path))
            with open(self.path, 'w') as fp:
                fp.write(response.read())

    def __init__(self, distribution, series, fetch=True):
        self.path = "boottest/images/{}/{}/manifest".format(
            distribution, series)

        if fetch:
            self.__fetch_manifest(distribution, series)

        self._manifest = self._load()

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


class BootTestJenkinsJob(object):
    """Boottest - Jenkins **glue**.

    Wraps 'boottest/jenkins/boottest-britney' script for:

    * 'check' existing boottest job status ('check <source> <version>')
    * 'submit' new boottest jobs ('submit <source> <version>')

    """

    script_path = "boottest/jenkins/boottest-britney"

    def __init__(self, distribution, series):
        self.distribution = distribution
        self.series = series

    def _run(self, *args):
        if not os.path.exists(self.script_path):
            print("E: [%s] - Boottest/Jenking glue script missing: %s" % (
                time.asctime(), self.script_path))
            return '-'
        command = [
            self.script_path,
            "-d", self.distribution, "-s", self.series,
            ]
        command.extend(args)
        return subprocess.check_output(command).strip()

    def get_status(self, name, version):
        """Return the current boottest jenkins job status.

        Request a boottest attempt if it's new.
        """
        try:
            status = self._run('check', name, version)
        except subprocess.CalledProcessError as err:
            status = self._run('submit', name, version)
        return status


class BootTest(object):
    """Boottest criteria for Britney.

    Process (update) excuses for the 'boottest' criteria. Request and monitor
    boottest attempts (see `BootTestJenkinsJob`) for binaries present in the
    phone image manifest (see `TouchManifest`).
    """
    VALID_STATUSES = ('PASS', 'SKIPPED')

    EXCUSE_LABELS = {
        "PASS": '<span style="background:#87d96c">Pass</span>',
        "SKIPPED": '<span style="background:#e5c545">Skipped</span>',
        "FAIL": '<span style="background:#ff6666">Regression</span>',
        "RUNNING": '<span style="background:#99ddff">Test in progress</span>',
    }

    def __init__(self, britney, distribution, series, debug=False):
        self.britney = britney
        self.distribution = distribution
        self.series = series
        self.debug = debug
        manifest_fetch = getattr(
            self.britney.options, "boottest_fetch", "no") == "yes"
        self.phone_manifest = TouchManifest(
            self.distribution, self.series, fetch=manifest_fetch)
        self.dispatcher = BootTestJenkinsJob(self.distribution, self.series)

    def update(self, excuse):
        """Return the boottest status for the given excuse.

        A new boottest job will be requested if the the source was not
        yet processed, otherwise the status of the corresponding job will
        be returned.

        Sources are only considered for boottesting if they produce binaries
        that are part of the phone image manifest. See `TouchManifest`.
        """
        # Discover all binaries for the 'excused' source.
        unstable_sources = self.britney.sources['unstable']

        # Dismiss if source is not yet recognized (??).
        if excuse.name not in unstable_sources:
            return None

        # Binaries are a seq of "<binname>/<arch>" and, practically, boottest
        # is only concerned about armhf binaries mentioned in the phone
        # manifest. Anything else should be skipped.
        phone_binaries = [
            b for b in unstable_sources[excuse.name][BINARIES]
            if b.split('/')[1] in self.britney.options.boottest_arches.split()
            and b.split('/')[0] in self.phone_manifest
        ]

        # Process (request or update) a boottest attempt for the source
        # if one or more of its binaries are part of the phone image.
        if phone_binaries:
            status = self.dispatcher.get_status(excuse.name, excuse.ver[1])
        else:
            status = 'SKIPPED'

        return status
