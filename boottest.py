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


class BootTest(object):
    """Boottest criteria for Britney.

    TBD!
    """

    def __init__(self, britney, distribution, series, debug=False):
        self.britney = britney
        self.distribution = distribution
        self.series = series
        self.debug = debug


    def _source_in_image(self, name):
        """Whether or not the given source name is in the phone image."""
        # XXX cprov 20150120: replace with a phone image manifest/content
        # check.
        if name == 'apache2':
            return False

        return True

    def _get_status_label(self, name, version):
        """Return the current boottest status label."""
        # XXX cprov 20150120: replace with the test history latest
        # record label.
        if name == 'pyqt5':
            if version == '1.1~beta':
                return 'PASS'
            return 'FAIL'

        return 'IN PROGRESS'

    def update(self, excuse):
        """Update given 'excuse' and return True if it has failed.

        Annotate skipped packages (currently not in phone image) or add
        the current testing status (see `_get_status_label`).
        """
        if not self._source_in_image(excuse.name):
            label = 'SKIPPED'
        else:
            label = self._get_status_label(excuse.name, excuse.ver[1])

        excuse.addhtml("boottest for %s %s: %s" %
                       (excuse.name, excuse.ver[1], label))

        if label in ['PASS', 'SKIPPED']:
            return False

        excuse.addhtml("Not considered")
        excuse.addreason("boottest")
        excuse.is_valid = False
        return True
