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

    def check(self, excuse):
        """Check and update given 'excuse' and return its label."""
        label = 'IN PROGRESS'
        # XXX cprov 20150120: replace with a phone image manifest/content
        # check.
        if excuse.name == 'apache2':
            label = 'SKIPPED'
            excuse.is_valid = False

        return label
