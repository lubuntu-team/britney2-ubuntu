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
import os

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

    def __init__(self, distribution, series):
        self.path = 'boottest/images/{}/{}/manifest'.format(
            distribution, series)
        self._manifest = self._load()

    def _load(self):
        pkg_list = []

        if not os.path.exists(self.path):
            return pkg_list

        with open(self.path) as fd:
            for line in fd.readlines():
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

    TBD!
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
        self.phone_manifest = TouchManifest(self.distribution, self.series)

    def _get_status(self, name, version):
        """Return the current boottest status.

        Request a boottest attempt if it's new.
        """
        # XXX cprov 20150120: replace with the test history latest
        # record label, or a new job request if it was not found.
        if name == 'pyqt5':
            if version == '1.1~beta':
                return 'PASS'
            return 'FAIL'

        return 'RUNNING'

    def update(self, excuse):
        """Update given 'excuse' and yields testing status.

        Yields (status, binary_name) for each binary considered for the
        given excuse. See `_get_status()`.

        Binaries are considered for boottesting if they are part of the
        phone image manifest. See `TouchManifest`.
        """
        # Discover all binaries for the 'excused' source.
        unstable_sources = self.britney.sources['unstable']
        # XXX cprov 20150120: binaries are a seq of "<binname>/<arch>" and,
        # practically, boottest is only concerned about armhf+all binaries.
        # Anything else should be skipped.
        binary_names = [
            bin.split('/')[0]
            for bin in unstable_sources[excuse.name][BINARIES]
        ]

        # Process (request or update) boottest attempts for each binary.
        for name in binary_names:
            if name in self.phone_manifest:
                status = self._get_status(name, excuse.ver[1])
            else:
                status = 'SKIPPED'
            yield name, status
