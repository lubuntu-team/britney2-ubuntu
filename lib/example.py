#!/usr/bin/env python

import sys
import britney

# VERSION = 0
# SECTION = 1
# SOURCE = 2
# SOURCEVER = 3
# ARCHITECTURE = 4
# MULTIARCH = 5
# PREDEPENDS = 6
# DEPENDS = 7
# CONFLICTS = 8
# PROVIDES = 9
# RDEPENDS = 10
# RCONFLICTS = 11

packages = {'phpldapadmin': ['1.0', 'web', 'phpldapadmin', '1.0', 'all', None, '', 'apache2 (>= 2.0)', '', '', [], []],
            'apache2': ['2.0', 'web', 'apache2', '2.0', 'i386', None, '', '', 'phpldapadmin (<= 1.0~)', '', [], []],
           }

system = britney.buildSystem('i386', packages)
print system.is_installable('phpldapadmin'), system.packages
system.remove_binary('apache2')
print system.is_installable('phpldapadmin'), system.packages
system.add_binary('apache2', ['2.0', 'web', 'apache2', '2.0', 'i386', None, '', '', 'phpldapadmin (<= 1.0~)', '', [], []])
print system.is_installable('phpldapadmin'), system.packages
