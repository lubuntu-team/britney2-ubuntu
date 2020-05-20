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

from britney2.utils import UbuntuComponent  # noqa: E402


class UtilTests(unittest.TestCase):
    def test_get_component(self):
        self.assertEqual(
            UbuntuComponent.get_component("utils"), UbuntuComponent.MAIN
        )
        self.assertEqual(
            UbuntuComponent.get_component("utils"), UbuntuComponent.MAIN
        )
        self.assertEqual(
            UbuntuComponent.get_component("restricted/admin"),
            UbuntuComponent.RESTRICTED,
        )
        self.assertEqual(
            UbuntuComponent.get_component("universe/web"),
            UbuntuComponent.UNIVERSE,
        )
        self.assertEqual(
            UbuntuComponent.get_component("multiverse/libs"),
            UbuntuComponent.MULTIVERSE,
        )

    def test_allowed_component(self):
        allowed_component = UbuntuComponent.allowed_component

        self.assertTrue(
            allowed_component(UbuntuComponent.MAIN, UbuntuComponent.MAIN)
        )
        self.assertFalse(
            allowed_component(UbuntuComponent.MAIN, UbuntuComponent.UNIVERSE)
        )
        self.assertFalse(
            allowed_component(UbuntuComponent.MAIN, UbuntuComponent.MULTIVERSE)
        )
        self.assertFalse(
            allowed_component(UbuntuComponent.MAIN, UbuntuComponent.RESTRICTED)
        )

        self.assertTrue(
            allowed_component(UbuntuComponent.RESTRICTED, UbuntuComponent.MAIN)
        )
        self.assertFalse(
            allowed_component(
                UbuntuComponent.RESTRICTED, UbuntuComponent.UNIVERSE
            )
        )
        self.assertFalse(
            allowed_component(
                UbuntuComponent.RESTRICTED, UbuntuComponent.MULTIVERSE
            )
        )
        self.assertTrue(
            allowed_component(
                UbuntuComponent.RESTRICTED, UbuntuComponent.RESTRICTED
            )
        )

        self.assertTrue(
            allowed_component(UbuntuComponent.UNIVERSE, UbuntuComponent.MAIN)
        )
        self.assertTrue(
            allowed_component(
                UbuntuComponent.UNIVERSE, UbuntuComponent.UNIVERSE
            )
        )
        self.assertFalse(
            allowed_component(
                UbuntuComponent.UNIVERSE, UbuntuComponent.MULTIVERSE
            )
        )
        self.assertFalse(
            allowed_component(
                UbuntuComponent.UNIVERSE, UbuntuComponent.RESTRICTED
            )
        )

        self.assertTrue(
            allowed_component(UbuntuComponent.MULTIVERSE, UbuntuComponent.MAIN)
        )
        self.assertTrue(
            allowed_component(
                UbuntuComponent.MULTIVERSE, UbuntuComponent.UNIVERSE
            )
        )
        self.assertTrue(
            allowed_component(
                UbuntuComponent.MULTIVERSE, UbuntuComponent.MULTIVERSE
            )
        )
        self.assertTrue(
            allowed_component(
                UbuntuComponent.MULTIVERSE, UbuntuComponent.RESTRICTED
            )
        )


if __name__ == "__main__":
    unittest.main()
