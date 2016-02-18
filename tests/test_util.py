#!/usr/bin/python3
# (C) 2014 - 2016 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import sys
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from britney_util import get_component, allowed_component
from consts import MAIN, RESTRICTED, UNIVERSE, MULTIVERSE


class UtilTests(unittest.TestCase):

    def test_get_component(self):
        self.assertEqual(get_component('utils'), MAIN)
        self.assertEqual(get_component('utils'), MAIN)
        self.assertEqual(get_component('restricted/admin'), RESTRICTED)
        self.assertEqual(get_component('universe/web'), UNIVERSE)
        self.assertEqual(get_component('multiverse/libs'), MULTIVERSE)

    def test_allowed_component(self):
        self.assertTrue(allowed_component(MAIN, MAIN))
        self.assertFalse(allowed_component(MAIN, UNIVERSE))
        self.assertFalse(allowed_component(MAIN, MULTIVERSE))
        self.assertFalse(allowed_component(MAIN, RESTRICTED))

        self.assertTrue(allowed_component(RESTRICTED, MAIN))
        self.assertFalse(allowed_component(RESTRICTED, UNIVERSE))
        self.assertFalse(allowed_component(RESTRICTED, MULTIVERSE))
        self.assertTrue(allowed_component(RESTRICTED, RESTRICTED))

        self.assertTrue(allowed_component(UNIVERSE, MAIN))
        self.assertTrue(allowed_component(UNIVERSE, UNIVERSE))
        self.assertFalse(allowed_component(UNIVERSE, MULTIVERSE))
        self.assertFalse(allowed_component(UNIVERSE, RESTRICTED))

        self.assertTrue(allowed_component(MULTIVERSE, MAIN))
        self.assertTrue(allowed_component(MULTIVERSE, UNIVERSE))
        self.assertTrue(allowed_component(MULTIVERSE, MULTIVERSE))
        self.assertTrue(allowed_component(MULTIVERSE, RESTRICTED))


if __name__ == '__main__':
    unittest.main()
