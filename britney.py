#!/usr/bin/python3 -u
# -*- coding: utf-8 -*-

# Copyright (C) 2001-2008 Anthony Towns <ajt@debian.org>
#                         Andreas Barth <aba@debian.org>
#                         Fabio Tranchitella <kobold@debian.org>
# Copyright (C) 2010-2013 Adam D. Barratt <adsb@debian.org>

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

"""
= Introduction =

This is the Debian testing updater script, also known as "Britney".

Packages are usually installed into the `testing' distribution after
they have undergone some degree of testing in unstable. The goal of
this software is to do this task in a smart way, allowing testing
to always be fully installable and close to being a release candidate.

Britney's source code is split between two different but related tasks:
the first one is the generation of the update excuses, while the
second tries to update testing with the valid candidates; first
each package alone, then larger and even larger sets of packages
together. Each try is accepted if testing is not more uninstallable
after the update than before.

= Data Loading =

In order to analyze the entire Debian distribution, Britney needs to
load in memory the whole archive: this means more than 10.000 packages
for twelve architectures, as well as the dependency interconnections
between them. For this reason, the memory requirements for running this
software are quite high and at least 1 gigabyte of RAM should be available.

Britney loads the source packages from the `Sources' file and the binary
packages from the `Packages_${arch}' files, where ${arch} is substituted
with the supported architectures. While loading the data, the software
analyzes the dependencies and builds a directed weighted graph in memory
with all the interconnections between the packages (see Britney.read_sources
and Britney.read_binaries).

Other than source and binary packages, Britney loads the following data:

  * BugsV, which contains the list of release-critical bugs for a given
    version of a source or binary package (see RCBugPolicy.read_bugs).

  * Dates, which contains the date of the upload of a given version
    of a source package (see Britney.read_dates).

  * Urgencies, which contains the urgency of the upload of a given
    version of a source package (see AgePolicy._read_urgencies).

  * Hints, which contains lists of commands which modify the standard behaviour
    of Britney (see Britney.read_hints).

  * Blocks, which contains user-supplied blocks read from Launchpad bugs
    (see LPBlockBugPolicy).

  * ExcuseBugs, which contains user-supplied update-excuse read from
    Launchpad bugs (see LPExcuseBugsPolicy).

For a more detailed explanation about the format of these files, please read
the documentation of the related methods. The exact meaning of them will be
instead explained in the chapter "Excuses Generation".

= Excuses =

An excuse is a detailed explanation of why a package can or cannot
be updated in the testing distribution from a newer package in
another distribution (like for example unstable). The main purpose
of the excuses is to be written in an HTML file which will be
published over HTTP. The maintainers will be able to parse it manually
or automatically to find the explanation of why their packages have
been updated or not.

== Excuses generation ==

These are the steps (with references to method names) that Britney
does for the generation of the update excuses.

 * If a source package is available in testing but it is not
   present in unstable and no binary packages in unstable are
   built from it, then it is marked for removal.

 * Every source package in unstable and testing-proposed-updates,
   if already present in testing, is checked for binary-NMUs, new
   or dropped binary packages in all the supported architectures
   (see Britney.should_upgrade_srcarch). The steps to detect if an
   upgrade is needed are:

    1. If there is a `remove' hint for the source package, the package
       is ignored: it will be removed and not updated.

    2. For every binary package built from the new source, it checks
       for unsatisfied dependencies, new binary packages and updated
       binary packages (binNMU), excluding the architecture-independent
       ones, and packages not built from the same source.

    3. For every binary package built from the old source, it checks
       if it is still built from the new source; if this is not true
       and the package is not architecture-independent, the script
       removes it from testing.

    4. Finally, if there is something worth doing (eg. a new or updated
       binary package) and nothing wrong it marks the source package
       as "Valid candidate", or "Not considered" if there is something
       wrong which prevented the update.

 * Every source package in unstable and testing-proposed-updates is
   checked for upgrade (see Britney.should_upgrade_src). The steps
   to detect if an upgrade is needed are:

    1. If the source package in testing is more recent the new one
       is ignored.

    2. If the source package doesn't exist (is fake), which means that
       a binary package refers to it but it is not present in the
       `Sources' file, the new one is ignored.

    3. If the package doesn't exist in testing, the urgency of the
       upload is ignored and set to the default (actually `low').

    4. If there is a `remove' hint for the source package, the package
       is ignored: it will be removed and not updated.

    5. If there is a `block' hint for the source package without an
       `unblock` hint or a `block-all source`, the package is ignored.

    6. If there is a `block-udeb' hint for the source package, it will
       have the same effect as `block', but may only be cancelled by
       a subsequent `unblock-udeb' hint.

    7. If the suite is unstable, the update can go ahead only if the
       upload happened more than the minimum days specified by the
       urgency of the upload; if this is not true, the package is
       ignored as `too-young'. Note that the urgency is sticky, meaning
       that the highest urgency uploaded since the previous testing
       transition is taken into account.

    8. If the suite is unstable, all the architecture-dependent binary
       packages and the architecture-independent ones for the `nobreakall'
       architectures have to be built from the source we are considering.
       If this is not true, then these are called `out-of-date'
       architectures and the package is ignored.

    9. The source package must have at least one binary package, otherwise
       it is ignored.

   10. If the suite is unstable, the new source package must have no
       release critical bugs which do not also apply to the testing
       one. If this is not true, the package is ignored as `buggy'.

   11. If there is a `force' hint for the source package, then it is
       updated even if it is marked as ignored from the previous steps.

   12. If the suite is {testing-,}proposed-updates, the source package can
       be updated only if there is an explicit approval for it.  Unless
       a `force' hint exists, the new package must also be available
       on all of the architectures for which it has binary packages in
       testing.

   13. If the package will be ignored, mark it as "Valid candidate",
       otherwise mark it as "Not considered".

 * The list of `remove' hints is processed: if the requested source
   package is not already being updated or removed and the version
   actually in testing is the same specified with the `remove' hint,
   it is marked for removal.

 * The excuses are sorted by the number of days from the last upload
   (days-old) and by name.

 * A list of unconsidered excuses (for which the package is not upgraded)
   is built. Using this list, all of the excuses depending on them are
   marked as invalid "impossible dependencies".

 * The excuses are written in an HTML file.
"""
import contextlib
import logging
import optparse
import os
import sys
import time
from collections import defaultdict
from functools import reduce
from itertools import chain
from operator import attrgetter

import apt_pkg

from britney2 import SourcePackage, BinaryPackageId, BinaryPackage
from britney2.excusefinder import ExcuseFinder
from britney2.hints import HintParser
from britney2.inputs.suiteloader import DebMirrorLikeSuiteContentLoader, MissingRequiredConfigurationError
from britney2.installability.builder import build_installability_tester
from britney2.installability.solver import InstallabilitySolver
from britney2.migration import MigrationManager
from britney2.migrationitem import MigrationItemFactory
from britney2.policies.policy import (AgePolicy,
                                      RCBugPolicy,
                                      PiupartsPolicy,
                                      DependsPolicy,
                                      BuildDependsPolicy,
                                      PolicyEngine,
                                      BlockPolicy,
                                      BuiltUsingPolicy,
                                      BuiltOnBuilddPolicy,
                                      ImplicitDependencyPolicy,
                                      LinuxPolicy,
                                      LPBlockBugPolicy,
                                      )
from britney2.policies.autopkgtest import AutopkgtestPolicy
from britney2.policies.sourceppa import SourcePPAPolicy
from britney2.policies.sruadtregression import SRUADTRegressionPolicy
from britney2.policies.email import EmailPolicy
from britney2.policies.lpexcusebugs import LPExcuseBugsPolicy
from britney2.utils import (log_and_format_old_libraries,
                            read_nuninst, write_nuninst, write_heidi,
                            format_and_log_uninst, newly_uninst,
                            write_excuses, write_heidi_delta,
                            old_libraries, is_nuninst_asgood_generous,
                            clone_nuninst, compile_nuninst, parse_provides,
                            MigrationConstraintException,
                            )

__author__ = 'Fabio Tranchitella and the Debian Release Team'
__version__ = '2.0'

# "temporarily" raise recursion limit for the auto hinter
sys.setrecursionlimit(2000)

class Britney(object):
    """Britney, the Debian testing updater script

    This is the script that updates the testing distribution. It is executed
    each day after the installation of the updated packages. It generates the
    `Packages' files for the testing distribution, but it does so in an
    intelligent manner; it tries to avoid any inconsistency and to use only
    non-buggy packages.

    For more documentation on this script, please read the Developers Reference.
    """

    HINTS_HELPERS = ("easy", "hint", "remove", "block", "block-udeb", "unblock", "unblock-udeb", "approve",
                     "remark", "ignore-piuparts", "ignore-rc-bugs", "force-skiptest", "force-badtest")
    HINTS_STANDARD = ("urgent", "age-days") + HINTS_HELPERS
    # ALL = {"force", "force-hint", "block-all"} | HINTS_STANDARD | registered policy hints (not covered above)
    HINTS_ALL = ('ALL')

    def __init__(self):
        """Class constructor

        This method initializes and populates the data lists, which contain all
        the information needed by the other methods of the class.
        """

        # setup logging - provide the "short level name" (i.e. INFO -> I) that
        # we used to use prior to using the logging module.

        old_factory = logging.getLogRecordFactory()
        short_level_mapping = {
            'CRITICAL': 'F',
            'INFO': 'I',
            'WARNING': 'W',
            'ERROR': 'E',
            'DEBUG': 'N',
        }

        def record_factory(*args, **kwargs):   # pragma: no cover
            record = old_factory(*args, **kwargs)
            try:
                record.shortlevelname = short_level_mapping[record.levelname]
            except KeyError:
                record.shortlevelname = record.levelname
            return record

        logging.setLogRecordFactory(record_factory)
        logging.basicConfig(format='{shortlevelname}: [{asctime}] - {message}',
                            style='{',
                            datefmt="%Y-%m-%dT%H:%M:%S%z",
                            stream=sys.stdout,
                            )

        self.logger = logging.getLogger()

        # Logger for "upgrade_output"; the file handler will be attached later when
        # we are ready to open the file.
        self.output_logger = logging.getLogger('britney2.output.upgrade_output')
        self.output_logger.setLevel(logging.INFO)

        # initialize the apt_pkg back-end
        apt_pkg.init()
        apt_pkg.init_config()

        # parse the command line arguments
        self._policy_engine = PolicyEngine()
        self.suite_info = None  # Initialized during __parse_arguments
        self.__parse_arguments()

        self.all_selected = []
        self.excuses = {}
        self.upgrade_me = []

        if self.options.nuninst_cache:
            self.logger.info("Not building the list of non-installable packages, as requested")
            if self.options.print_uninst:
                nuninst = read_nuninst(self.options.noninst_status,
                                       self.options.architectures)
                print('* summary')
                print('\n'.join('%4d %s' % (len(nuninst[x]), x) for x in self.options.architectures))
                return

        try:
            constraints_file = os.path.join(self.options.static_input_dir, 'constraints')
            faux_packages = os.path.join(self.options.static_input_dir, 'faux-packages')
        except AttributeError:
            self.logger.info("The static_input_dir option is not set")
            constraints_file = None
            faux_packages = None
        if faux_packages is not None and os.path.exists(faux_packages):
            self.logger.info("Loading faux packages from %s", faux_packages)
            self._load_faux_packages(faux_packages)
        elif faux_packages is not None:
            self.logger.info("No Faux packages as %s does not exist", faux_packages)

        if constraints_file is not None and os.path.exists(constraints_file):
            self.logger.info("Loading constraints from %s", constraints_file)
            self.constraints = self._load_constraints(constraints_file)
        else:
            if constraints_file is not None:
                self.logger.info("No constraints as %s does not exist", constraints_file)
            self.constraints = {
                'keep-installable': [],
            }

        self.logger.info("Compiling Installability tester")
        self.pkg_universe, self._inst_tester = build_installability_tester(self.suite_info, self.options.architectures)
        target_suite = self.suite_info.target_suite
        target_suite.inst_tester = self._inst_tester

        self.allow_uninst = {}
        for arch in self.options.architectures:
            self.allow_uninst[arch] = set()
        self._migration_item_factory = MigrationItemFactory(self.suite_info)
        self._hint_parser = HintParser(self._migration_item_factory)
        self._migration_manager = MigrationManager(self.options, self.suite_info, self.all_binaries, self.pkg_universe,
                                                   self.constraints, self.allow_uninst, self._migration_item_factory,
                                                   self.hints)

        if not self.options.nuninst_cache:
            self.logger.info("Building the list of non-installable packages for the full archive")
            self._inst_tester.compute_installability()
            nuninst = compile_nuninst(target_suite,
                                      self.options.architectures,
                                      self.options.nobreakall_arches)
            self.nuninst_orig = nuninst
            for arch in self.options.architectures:
                self.logger.info("> Found %d non-installable packages", len(nuninst[arch]))
                if self.options.print_uninst:
                    self.nuninst_arch_report(nuninst, arch)

            if self.options.print_uninst:
                print('* summary')
                print('\n'.join(map(lambda x: '%4d %s' % (len(nuninst[x]), x), self.options.architectures)))
                return
            else:
                write_nuninst(self.options.noninst_status, nuninst)

            stats = self._inst_tester.compute_stats()
            self.logger.info("> Installability tester statistics (per architecture)")
            for arch in self.options.architectures:
                arch_stat = stats[arch]
                self.logger.info(">  %s", arch)
                for stat in arch_stat.stat_summary():
                    self.logger.info(">  - %s", stat)
        else:
            self.logger.info("Loading uninstallability counters from cache")
            self.nuninst_orig = read_nuninst(self.options.noninst_status,
                                             self.options.architectures)

        # nuninst_orig may get updated during the upgrade process
        self.nuninst_orig_save = clone_nuninst(self.nuninst_orig, architectures=self.options.architectures)

        self._policy_engine.register_policy_hints(self._hint_parser)

        try:
            self.read_hints(self.options.hintsdir)
        except AttributeError:
            self.read_hints(os.path.join(self.suite_info['unstable'].path, 'Hints'))

        self._policy_engine.initialise(self, self.hints)

    def __parse_arguments(self):
        """Parse the command line arguments

        This method parses and initializes the command line arguments.
        While doing so, it preprocesses some of the options to be converted
        in a suitable form for the other methods of the class.
        """
        # initialize the parser
        parser = optparse.OptionParser(version="%prog")
        parser.add_option("-v", "", action="count", dest="verbose", help="enable verbose output")
        parser.add_option("-c", "--config", action="store", dest="config", default="/etc/britney.conf",
                          help="path for the configuration file")
        parser.add_option("", "--architectures", action="store", dest="architectures", default=None,
                          help="override architectures from configuration file")
        parser.add_option("", "--actions", action="store", dest="actions", default=None,
                          help="override the list of actions to be performed")
        parser.add_option("", "--hints", action="store", dest="hints", default=None,
                          help="additional hints, separated by semicolons")
        parser.add_option("", "--hint-tester", action="store_true", dest="hint_tester", default=None,
                          help="provide a command line interface to test hints")
        parser.add_option("", "--dry-run", action="store_true", dest="dry_run", default=False,
                          help="disable all outputs to the testing directory")
        parser.add_option("", "--nuninst-cache", action="store_true", dest="nuninst_cache", default=False,
                          help="do not build the non-installability status, use the cache from file")
        parser.add_option("", "--print-uninst", action="store_true", dest="print_uninst", default=False,
                          help="just print a summary of uninstallable packages")
        parser.add_option("", "--compute-migrations", action="store_true", dest="compute_migrations", default=True,
                          help="Compute which packages can migrate (the default)")
        parser.add_option("", "--no-compute-migrations", action="store_false", dest="compute_migrations",
                          help="Do not compute which packages can migrate.")
        parser.add_option("", "--series", action="store", dest="series", default='',
                          help="set distribution series name")
        parser.add_option("", "--distribution", action="store", dest="distribution", default="Debian",
                          help="set distribution name")
        (self.options, self.args) = parser.parse_args()

        if self.options.verbose:
            self.logger.setLevel(logging.INFO)
        else:
            self.logger.setLevel(logging.WARNING)
        # TODO: Define a more obvious toggle for debug information
        try:  # pragma: no cover
            if int(os.environ.get('BRITNEY_DEBUG', '0')):
                self.logger.setLevel(logging.DEBUG)
        except ValueError:  # pragma: no cover
            pass

        # integrity checks
        # if the configuration file exists, then read it and set the additional options
        if not os.path.isfile(self.options.config):  # pragma: no cover
            self.logger.error("Unable to read the configuration file (%s), exiting!", self.options.config)
            sys.exit(1)

        # minimum days for unstable-testing transition and the list of hints
        # are handled as an ad-hoc case
        MINDAYS = {}

        self.HINTS = {'command-line': self.HINTS_ALL}
        with open(self.options.config, encoding='utf-8') as config:
            for line in config:
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip()
                    if self.options.series is not None:
                        v = v.replace("%(SERIES)", self.options.series)
                    if k.startswith("MINDAYS_"):
                        MINDAYS[k.split("_")[1].lower()] = int(v)
                    elif k.startswith("HINTS_"):
                        self.HINTS[k.split("_")[1].lower()] = \
                            reduce(lambda x, y: x+y, [
                                hasattr(self, "HINTS_" + i) and
                                getattr(self, "HINTS_" + i) or
                                (i,) for i in v.split()])
                    elif not hasattr(self.options, k.lower()) or \
                            not getattr(self.options, k.lower()):
                        setattr(self.options, k.lower(), v)

        if hasattr(self.options, 'components'):  # pragma: no cover
            self.logger.error("The COMPONENTS configuration has been removed.")
            self.logger.error("Britney will read the value from the Release file automatically")
            sys.exit(1)

        suite_loader = DebMirrorLikeSuiteContentLoader(self.options)

        try:
            self.suite_info = suite_loader.load_suites()
        except MissingRequiredConfigurationError as e:   # pragma: no cover
            self.logger.error("Could not load the suite content due to missing configuration: %s", str(e))
            sys.exit(1)
        self.all_binaries = suite_loader.all_binaries()
        self.options.components = suite_loader.components
        self.options.architectures = suite_loader.architectures
        self.options.nobreakall_arches = suite_loader.nobreakall_arches
        self.options.outofsync_arches = suite_loader.outofsync_arches
        self.options.break_arches = suite_loader.break_arches
        self.options.new_arches = suite_loader.new_arches
        if self.options.series == '':
            self.options.series = self.suite_info.target_suite.name

        if hasattr(self.options, 'heidi_output') and not hasattr(self.options, "heidi_delta_output"):
            self.options.heidi_delta_output = self.options.heidi_output + "Delta"

        self.options.smooth_updates = self.options.smooth_updates.split()

        if not hasattr(self.options, 'ignore_cruft') or \
                self.options.ignore_cruft == "0":
            self.options.ignore_cruft = False

        if not hasattr(self.options, 'check_consistency_level'):
            self.options.check_consistency_level = 2
        else:
            self.options.check_consistency_level = int(self.options.check_consistency_level)

        if not hasattr(self.options, 'adt_retry_url_mech'):
            self.options.adt_retry_url_mech = ''

        self.options.has_arch_all_buildds = getattr(self.options, 'has_arch_all_buildds', 'yes') == 'yes'

        self._policy_engine.add_policy(DependsPolicy(self.options, self.suite_info))
        self._policy_engine.add_policy(RCBugPolicy(self.options, self.suite_info))
        if getattr(self.options, 'piuparts_enable', 'yes') == 'yes':
            self._policy_engine.add_policy(PiupartsPolicy(self.options, self.suite_info))
        add_autopkgtest_policy = getattr(self.options, 'adt_enable', 'no')
        if add_autopkgtest_policy in ('yes', 'dry-run'):
            self._policy_engine.add_policy(AutopkgtestPolicy(self.options, self.suite_info, dry_run=add_autopkgtest_policy == 'dry-run'))
        self._policy_engine.add_policy(AgePolicy(self.options, self.suite_info, MINDAYS))
        # XXX this policy results in asymmetric enforcement of
        # build-dependencies in the release pocket (nothing blocks
        # propagation of a new package which will regress build-dep
        # satisfaction of another package already in the release pocket, this
        # only shows up via the NBS report); and this is a subset of what we
        # already get from the rebuild tests, which the reality is we don't
        # have capacity to enforce for the whole archive. - vorlon
        # self._policy_engine.add_policy(BuildDependsPolicy(self.options, self.suite_info))
        self._policy_engine.add_policy(BlockPolicy(self.options, self.suite_info))
        # XXX re-enable once https://bugs.launchpad.net/launchpad/+bug/1868558 is fixed
        # self._policy_engine.add_policy(BuiltUsingPolicy(self.options, self.suite_info))
        # This policy is objectively terrible, it reduces britney runtime
        # at the cost of giving only partial information about blocking
        # packages during large transitions instead of giving full detail in
        # update_output that the team can work through in parallel, thus
        # dragging out complicated transitions. vorlon 20240214
        if getattr(self.options, 'implicit_deps', 'yes') == 'yes':
            self._policy_engine.add_policy(ImplicitDependencyPolicy(self.options, self.suite_info))
        if getattr(self.options, 'check_buildd', 'no') == 'yes':
            self._policy_engine.add_policy(BuiltOnBuilddPolicy(self.options, self.suite_info))
        self._policy_engine.add_policy(LPBlockBugPolicy(self.options, self.suite_info))
        self._policy_engine.add_policy(LPExcuseBugsPolicy(self.options, self.suite_info))
        self._policy_engine.add_policy(SourcePPAPolicy(self.options, self.suite_info))
        self._policy_engine.add_policy(LinuxPolicy(self.options, self.suite_info))
        add_sruregression_policy = getattr(self.options, 'sruregressionemail_enable', 'no')
        if add_sruregression_policy in ('yes', 'dry-run'):
            self._policy_engine.add_policy(SRUADTRegressionPolicy(self.options,
                                                                  self.suite_info,
                                                                  dry_run=add_sruregression_policy == 'dry-run'))
        add_email_policy = getattr(self.options, 'email_enable', 'no')
        if add_email_policy in ('yes', 'dry-run'):
            self._policy_engine.add_policy(EmailPolicy(self.options,
                                                       self.suite_info,
                                                       dry_run=add_email_policy == 'dry-run'))

    @property
    def hints(self):
        return self._hint_parser.hints

    def _load_faux_packages(self, faux_packages_file):
        """Loads fake packages

        In rare cases, it is useful to create a "fake" package that can be used to satisfy
        dependencies.  This is usually needed for packages that are not shipped directly
        on this mirror but is a prerequisite for using this mirror (e.g. some vendors provide
        non-distributable "setup" packages and contrib/non-free packages depend on these).

        :param faux_packages_file: Path to the file containing the fake package definitions
        """
        tag_file = apt_pkg.TagFile(faux_packages_file)
        get_field = tag_file.section.get
        step = tag_file.step
        no = 0
        pri_source_suite = self.suite_info.primary_source_suite
        target_suite = self.suite_info.target_suite

        while step():
            no += 1
            pkg_name = get_field('Package', None)
            if pkg_name is None:  # pragma: no cover
                raise ValueError("Missing Package field in paragraph %d (file %s)" % (no, faux_packages_file))
            pkg_name = sys.intern(pkg_name)
            version = sys.intern(get_field('Version', '1.0-1'))
            provides_raw = get_field('Provides')
            archs_raw = get_field('Architecture', None)
            component = get_field('Component', 'non-free')
            if archs_raw:
                archs = archs_raw.split()
            else:
                archs = self.options.architectures
            faux_section = 'faux'
            if component != 'main':
                faux_section = "%s/faux" % component
            src_data = SourcePackage(pkg_name,
                                     version,
                                     sys.intern(faux_section),
                                     set(),
                                     None,
                                     True,
                                     None,
                                     None,
                                     [],
                                     [],
                                     )

            target_suite.sources[pkg_name] = src_data
            pri_source_suite.sources[pkg_name] = src_data

            for arch in archs:
                pkg_id = BinaryPackageId(pkg_name, version, arch)
                if provides_raw:
                    provides = parse_provides(provides_raw, pkg_id=pkg_id, logger=self.logger)
                else:
                    provides = []
                bin_data = BinaryPackage(version,
                                         faux_section,
                                         pkg_name,
                                         version,
                                         arch,
                                         get_field('Multi-Arch'),
                                         None,
                                         None,
                                         provides,
                                         False,
                                         pkg_id,
                                         [],
                                         )

                src_data.binaries.add(pkg_id)
                target_suite.binaries[arch][pkg_name] = bin_data
                pri_source_suite.binaries[arch][pkg_name] = bin_data

                # register provided packages with the target suite provides table
                for provided_pkg, provided_version, _ in bin_data.provides:
                    target_suite.provides_table[arch][provided_pkg].add((pkg_name, provided_version))

                self.all_binaries[pkg_id] = bin_data

    def _load_constraints(self, constraints_file):
        """Loads configurable constraints

        The constraints file can contain extra rules that Britney should attempt
        to satisfy.  Examples can be "keep package X in testing and ensure it is
        installable".

        :param constraints_file: Path to the file containing the constraints
        """
        tag_file = apt_pkg.TagFile(constraints_file)
        get_field = tag_file.section.get
        step = tag_file.step
        no = 0
        faux_version = sys.intern('1')
        faux_section = sys.intern('faux')
        keep_installable = []
        constraints = {
            'keep-installable': keep_installable
        }
        pri_source_suite = self.suite_info.primary_source_suite
        target_suite = self.suite_info.target_suite

        while step():
            no += 1
            pkg_name = get_field('Fake-Package-Name', None)
            if pkg_name is None:  # pragma: no cover
                raise ValueError("Missing Fake-Package-Name field in paragraph %d (file %s)" % (no, constraints_file))
            pkg_name = sys.intern(pkg_name)

            def mandatory_field(x):
                v = get_field(x, None)
                if v is None:  # pragma: no cover
                    raise ValueError("Missing %s field for %s (file %s)" % (x, pkg_name, constraints_file))
                return v

            constraint = mandatory_field('Constraint')
            if constraint not in {'present-and-installable'}:  # pragma: no cover
                raise ValueError("Unsupported constraint %s for %s (file %s)" % (constraint, pkg_name, constraints_file))

            self.logger.info(" - constraint %s", pkg_name)

            pkg_list = [x.strip() for x in mandatory_field('Package-List').split("\n")
                        if x.strip() != '' and not x.strip().startswith("#")]
            src_data = SourcePackage(pkg_name,
                                     faux_version,
                                     faux_section,
                                     set(),
                                     None,
                                     True,
                                     None,
                                     None,
                                     [],
                                     [],
                                     )
            target_suite.sources[pkg_name] = src_data
            pri_source_suite.sources[pkg_name] = src_data
            keep_installable.append(pkg_name)
            for arch in self.options.architectures:
                deps = []
                for pkg_spec in pkg_list:
                    s = pkg_spec.split(None, 1)
                    if len(s) == 1:
                        deps.append(s[0])
                    else:
                        pkg, arch_res = s
                        if not (arch_res.startswith('[') and arch_res.endswith(']')):  # pragma: no cover
                            raise ValueError("Invalid arch-restriction on %s - should be [arch1 arch2] (for %s file %s)"
                                             % (pkg, pkg_name, constraints_file))
                        arch_res = arch_res[1:-1].split()
                        if not arch_res:  # pragma: no cover
                            msg = "Empty arch-restriction for %s: Uses comma or negation (for %s file %s)"
                            raise ValueError(msg % (pkg, pkg_name, constraints_file))
                        for a in arch_res:
                            if a == arch:
                                deps.append(pkg)
                            elif ',' in a or '!' in a:  # pragma: no cover
                                msg = "Invalid arch-restriction for %s: Uses comma or negation (for %s file %s)"
                                raise ValueError(msg % (pkg, pkg_name, constraints_file))
                pkg_id = BinaryPackageId(pkg_name, faux_version, arch)
                bin_data = BinaryPackage(faux_version,
                                         faux_section,
                                         pkg_name,
                                         faux_version,
                                         arch,
                                         'no',
                                         ', '.join(deps),
                                         None,
                                         [],
                                         False,
                                         pkg_id,
                                         [],
                                         )
                src_data.binaries.add(pkg_id)
                target_suite.binaries[arch][pkg_name] = bin_data
                pri_source_suite.binaries[arch][pkg_name] = bin_data
                self.all_binaries[pkg_id] = bin_data

        return constraints

    # Data reading/writing methods
    # ----------------------------

    def read_hints(self, hintsdir):
        """Read the hint commands from the specified directory

        The hint commands are read from the files contained in the directory
        specified by the `hintsdir' parameter.
        The names of the files have to be the same as the authorized users
        for the hints.

        The file contains rows with the format:

        <command> <package-name>[/<version>]

        The method returns a dictionary where the key is the command, and
        the value is the list of affected packages.
        """

        for who in self.HINTS.keys():
            if who == 'command-line':
                lines = self.options.hints and self.options.hints.split(';') or ()
                filename = '<cmd-line>'
                self._hint_parser.parse_hints(who, self.HINTS[who], filename, lines)
            else:
                filename = os.path.join(hintsdir, who)
                if not os.path.isfile(filename):
                    self.logger.error("Cannot read hints list from %s, no such file!", filename)
                    continue
                self.logger.info("Loading hints list from %s", filename)
                with open(filename, encoding='utf-8') as f:
                    self._hint_parser.parse_hints(who, self.HINTS[who], filename, f)

        hints = self._hint_parser.hints

        for x in ["block", "block-all", "block-udeb", "unblock", "unblock-udeb", "force", "urgent", "remove", "age-days"]:
            z = defaultdict(dict)
            for hint in hints[x]:
                package = hint.package
                architecture = hint.architecture
                key = (hint, hint.user)
                if package in z and architecture in z[package] and z[package][architecture] != key:
                    hint2 = z[package][architecture][0]
                    if x in ['unblock', 'unblock-udeb']:
                        if apt_pkg.version_compare(hint2.version, hint.version) < 0:
                            # This hint is for a newer version, so discard the old one
                            self.logger.warning("Overriding %s[%s] = ('%s', '%s', '%s') with ('%s', '%s', '%s')",
                                                x, package, hint2.version, hint2.architecture,
                                                hint2.user, hint.version, hint.architecture, hint.user)
                            hint2.set_active(False)
                        else:
                            # This hint is for an older version, so ignore it in favour of the new one
                            self.logger.warning("Ignoring %s[%s] = ('%s', '%s', '%s'), ('%s', '%s', '%s') is higher or equal",
                                                x, package, hint.version, hint.architecture, hint.user,
                                                hint2.version, hint2.architecture, hint2.user)
                            hint.set_active(False)
                    else:
                        self.logger.warning("Overriding %s[%s] = ('%s', '%s') with ('%s', '%s')",
                                            x, package, hint2.user, hint2, hint.user, hint)
                        hint2.set_active(False)

                z[package][architecture] = key

        for hint in hints['allow-uninst']:
            if hint.architecture == 'source':
                for arch in self.options.architectures:
                    self.allow_uninst[arch].add(hint.package)
            else:
                self.allow_uninst[hint.architecture].add(hint.package)

        # Sanity check the hints hash
        if len(hints["block"]) == 0 and len(hints["block-udeb"]) == 0:
            self.logger.warning("WARNING: No block hints at all, not even udeb ones!")

    def write_excuses(self):
        """Produce and write the update excuses

        This method handles the update excuses generation: the packages are
        looked at to determine whether they are valid candidates. For the details
        of this procedure, please refer to the module docstring.
        """

        self.logger.info("Update Excuses generation started")

        mi_factory = self._migration_item_factory
        excusefinder = ExcuseFinder(self.options, self.suite_info, self.all_binaries,
                                    self.pkg_universe, self._policy_engine, mi_factory, self.hints)

        excuses, upgrade_me = excusefinder.find_actionable_excuses()
        self.excuses = excuses

        # sort the list of candidates
        self.upgrade_me = sorted(upgrade_me)
        old_lib_removals = old_libraries(mi_factory, self.suite_info, self.options.outofsync_arches)
        self.upgrade_me.extend(old_lib_removals)
        self.output_logger.info("List of old libraries added to upgrade_me (%d):", len(old_lib_removals))
        log_and_format_old_libraries(self.output_logger, old_lib_removals)

        # write excuses to the output file
        if not self.options.dry_run:
            self.logger.info("> Writing Excuses to %s", self.options.excuses_output)
            os.makedirs(os.path.dirname(self.options.excuses_output), exist_ok=True)
            write_excuses(excuses, self.options.excuses_output,
                          output_format="legacy-html")
            if hasattr(self.options, 'excuses_yaml_output'):
                self.logger.info("> Writing YAML Excuses to %s", self.options.excuses_yaml_output)
                write_excuses(excuses, self.options.excuses_yaml_output,
                              output_format="yaml")

        self.logger.info("Update Excuses generation completed")

    # Upgrade run
    # -----------

    def eval_nuninst(self, nuninst, original=None):
        """Return a string which represents the uninstallability counters

        This method returns a string which represents the uninstallability
        counters reading the uninstallability statistics `nuninst` and, if
        present, merging the results with the `original` one.

        An example of the output string is:
        1+2: i-0:a-0:a-0:h-0:i-1:m-0:m-0:p-0:a-0:m-0:s-2:s-0

        where the first part is the number of broken packages in non-break
        architectures + the total number of broken packages for all the
        architectures.
        """
        res = []
        total = 0
        totalbreak = 0
        for arch in self.options.architectures:
            if arch in nuninst:
                n = len(nuninst[arch])
            elif original and arch in original:
                n = len(original[arch])
            else:
                continue
            if arch in self.options.break_arches:
                totalbreak = totalbreak + n
            else:
                total = total + n
            res.append("%s-%d" % (arch[0], n))
        return "%d+%d: %s" % (total, totalbreak, ":".join(res))

    def iter_packages(self, packages, selected, nuninst=None):
        """Iter on the list of actions and apply them one-by-one

        This method applies the changes from `packages` to testing, checking the uninstallability
        counters for every action performed. If the action does not improve them, it is reverted.
        The method returns the new uninstallability counters and the remaining actions if the
        final result is successful, otherwise (None, []).
        """
        group_info = {}
        rescheduled_packages = packages
        maybe_rescheduled_packages = []
        output_logger = self.output_logger
        solver = InstallabilitySolver(self.pkg_universe, self._inst_tester)
        mm = self._migration_manager
        target_suite = self.suite_info.target_suite

        for y in sorted((y for y in packages), key=attrgetter('uvname')):
            try:
                _, updates, rms, _ = mm.compute_groups(y)
                result = (y, frozenset(updates), frozenset(rms))
                group_info[y] = result
            except MigrationConstraintException as e:
                rescheduled_packages.remove(y)
                output_logger.info("not adding package to list: %s", (y.package))
                output_logger.info("    got exception: %s" % (repr(e)))

        if nuninst:
            nuninst_orig = nuninst
        else:
            nuninst_orig = self.nuninst_orig

        nuninst_last_accepted = nuninst_orig

        output_logger.info("recur: [] %s %d/0", ",".join(x.uvname for x in selected), len(packages))
        while rescheduled_packages:
            groups = {group_info[x] for x in rescheduled_packages}
            worklist = solver.solve_groups(groups)
            rescheduled_packages = []

            worklist.reverse()

            while worklist:
                comp = worklist.pop()
                comp_name = ' '.join(item.uvname for item in comp)
                output_logger.info("trying: %s" % comp_name)
                with mm.start_transaction() as transaction:
                    accepted = False
                    try:
                        accepted, nuninst_after, failed_arch, new_cruft = mm.migrate_items_to_target_suite(
                            comp,
                            nuninst_last_accepted
                        )
                        if accepted:
                            selected.extend(comp)
                            transaction.commit()
                            output_logger.info("accepted: %s", comp_name)
                            output_logger.info("   ori: %s", self.eval_nuninst(nuninst_orig))
                            output_logger.info("   pre: %s", self.eval_nuninst(nuninst_last_accepted))
                            output_logger.info("   now: %s", self.eval_nuninst(nuninst_after))
                            if new_cruft:
                                output_logger.info(
                                    "   added new cruft items to list: %s",
                                    " ".join(x.uvname for x in new_cruft))

                            if len(selected) <= 20:
                                output_logger.info("   all: %s", " ".join(x.uvname for x in selected))
                            else:
                                output_logger.info("  most: (%d) .. %s",
                                                   len(selected),
                                                   " ".join(x.uvname for x in selected[-20:]))
                            if self.options.check_consistency_level >= 3:
                                target_suite.check_suite_source_pkg_consistency('iter_packages after commit')
                            nuninst_last_accepted = nuninst_after
                            for cruft_item in new_cruft:
                                try:
                                    _, updates, rms, _ = mm.compute_groups(cruft_item)
                                    result = (cruft_item, frozenset(updates), frozenset(rms))
                                    group_info[cruft_item] = result
                                    worklist.append([cruft_item])
                                except MigrationConstraintException as e:
                                    output_logger.info(
                                        "    got exception adding cruft item %s to list: %s" %
                                        (cruft_item.uvname, repr(e)))
                            rescheduled_packages.extend(maybe_rescheduled_packages)
                            maybe_rescheduled_packages.clear()
                        else:
                            transaction.rollback()
                            broken = sorted(b for b in nuninst_after[failed_arch]
                                            if b not in nuninst_last_accepted[failed_arch])
                            compare_nuninst = None
                            if any(item for item in comp if item.architecture != 'source'):
                                compare_nuninst = nuninst_last_accepted
                            # NB: try_migration already reverted this for us, so just print the results and move on
                            output_logger.info("skipped: %s (%d, %d, %d)",
                                               comp_name,
                                               len(rescheduled_packages),
                                               len(maybe_rescheduled_packages),
                                               len(worklist)
                                               )
                            output_logger.info("    got: %s", self.eval_nuninst(nuninst_after, compare_nuninst))
                            output_logger.info("    * %s: %s", failed_arch, ", ".join(broken))
                            if self.options.check_consistency_level >= 3:
                                target_suite.check_suite_source_pkg_consistency('iter_package after rollback (not accepted)')

                    except MigrationConstraintException as e:
                        transaction.rollback()
                        output_logger.info("skipped: %s (%d, %d, %d)",
                                           comp_name,
                                           len(rescheduled_packages),
                                           len(maybe_rescheduled_packages),
                                           len(worklist)
                                           )
                        output_logger.info("    got exception: %s" % (repr(e)))
                        if self.options.check_consistency_level >= 3:
                            target_suite.check_suite_source_pkg_consistency(
                                'iter_package after rollback (MigrationConstraintException)')

                    if not accepted:
                        if len(comp) > 1:
                            output_logger.info("    - splitting the component into single items and retrying them")
                            worklist.extend([item] for item in comp)
                        else:
                            maybe_rescheduled_packages.append(comp[0])

        output_logger.info(" finish: [%s]", ",".join(x.uvname for x in selected))
        output_logger.info("endloop: %s", self.eval_nuninst(self.nuninst_orig))
        output_logger.info("    now: %s", self.eval_nuninst(nuninst_last_accepted))
        format_and_log_uninst(output_logger,
                              self.options.architectures,
                              newly_uninst(self.nuninst_orig, nuninst_last_accepted)
                              )
        output_logger.info("")

        return (nuninst_last_accepted, maybe_rescheduled_packages)

    def do_all(self, hinttype=None, init=None, actions=None, break_ok=False):
        """Testing update runner

        This method tries to update testing checking the uninstallability
        counters before and after the actions to decide if the update was
        successful or not.
        """
        selected = []
        if actions:
            upgrade_me = actions[:]
        else:
            upgrade_me = self.upgrade_me[:]
        nuninst_start = self.nuninst_orig
        output_logger = self.output_logger
        target_suite = self.suite_info.target_suite

        # these are special parameters for hints processing
        force = False
        recurse = True
        nuninst_end = None
        extra = []
        mm = self._migration_manager

        if hinttype == "easy" or hinttype == "force-hint":
            force = hinttype == "force-hint"
            recurse = False

        # if we have a list of initial packages, check them
        if init:
            for x in init:
                if x not in upgrade_me:
                    output_logger.warning("failed: %s is not a valid candidate (or it already migrated)", x.uvname)
                    return None
                selected.append(x)
                upgrade_me.remove(x)

        output_logger.info("start: %s", self.eval_nuninst(nuninst_start))
        output_logger.info("orig: %s", self.eval_nuninst(nuninst_start))

        if init and not force:
            # We will need to be able to roll back (e.g. easy or a "hint"-hint)
            _start_transaction = mm.start_transaction
        else:
            # No "outer" transaction needed as we will never need to rollback
            # (e.g. "force-hint" or a regular "main run").  Emulate the start_transaction
            # call from the MigrationManager, so the rest of the code follows the
            # same flow regardless of whether we need the transaction or not.

            @contextlib.contextmanager
            def _start_transaction():
                yield None

        with _start_transaction() as transaction:

            if init:
                # init => a hint (e.g. "easy") - so do the hint run
                (_, nuninst_end, _, new_cruft) = mm.migrate_items_to_target_suite(selected,
                                                                                  self.nuninst_orig,
                                                                                  stop_on_first_regression=False)

                if recurse:
                    # Ensure upgrade_me and selected do not overlap, if we
                    # follow-up with a recurse ("hint"-hint).
                    upgrade_me = [x for x in upgrade_me if x not in set(selected)]
                else:
                    # On non-recursive hints check for cruft and purge it proactively in case it "fixes" the hint.
                    cruft = [x for x in upgrade_me if x.is_cruft_removal]
                    if new_cruft:
                        output_logger.info(
                            "Change added new cruft items to list: %s",
                            " ".join(x.uvname for x in new_cruft))
                        cruft.extend(new_cruft)
                    if cruft:
                        output_logger.info("Checking if changes enables cruft removal")
                        (nuninst_end, remaining_cruft) = self.iter_packages(cruft,
                                                                            selected,
                                                                            nuninst=nuninst_end)
                        output_logger.info("Removed %d of %d cruft item(s) after the changes",
                                           len(cruft) - len(remaining_cruft), len(cruft))
                        new_cruft.difference_update(remaining_cruft)

                # Add new cruft items regardless of whether we recurse.  A future run might clean
                # them for us.
                upgrade_me.extend(new_cruft)

            if recurse:
                # Either the main run or the recursive run of a "hint"-hint.
                (nuninst_end, extra) = self.iter_packages(upgrade_me,
                                                          selected,
                                                          nuninst=nuninst_end)

            nuninst_end_str = self.eval_nuninst(nuninst_end)

            if not recurse:
                # easy or force-hint
                output_logger.info("easy: %s", nuninst_end_str)

                if not force:
                    format_and_log_uninst(self.output_logger,
                                          self.options.architectures,
                                          newly_uninst(nuninst_start, nuninst_end)
                                          )

            if force:
                # Force implies "unconditionally better"
                better = True
            else:
                break_arches = set(self.options.break_arches)
                if all(x.architecture in break_arches for x in selected) and not break_ok:
                    # If we only migrated items from break-arches, then we
                    # do not allow any regressions on these architectures.
                    # This usually only happens with hints
                    break_arches = set()
                better = is_nuninst_asgood_generous(self.constraints,
                                                    self.allow_uninst,
                                                    self.options.architectures,
                                                    self.nuninst_orig,
                                                    nuninst_end,
                                                    break_arches)

            if better:
                # Result accepted either by force or by being better than the original result.
                output_logger.info("final: %s", ",".join(sorted(x.uvname for x in selected)))
                output_logger.info("start: %s", self.eval_nuninst(nuninst_start))
                output_logger.info(" orig: %s", self.eval_nuninst(self.nuninst_orig))
                output_logger.info("  end: %s", nuninst_end_str)
                if force:
                    broken = newly_uninst(nuninst_start, nuninst_end)
                    if broken:
                        output_logger.warning("force breaks:")
                        format_and_log_uninst(self.output_logger,
                                              self.options.architectures,
                                              broken,
                                              loglevel=logging.WARNING,
                                              )
                    else:
                        output_logger.info("force did not break any packages")
                output_logger.info("SUCCESS (%d/%d)", len(actions or self.upgrade_me), len(extra))
                self.nuninst_orig = nuninst_end
                self.all_selected += selected
                if transaction:
                    transaction.commit()
                    if self.options.check_consistency_level >= 2:
                        target_suite.check_suite_source_pkg_consistency('do_all after commit')
                if not actions:
                    if recurse:
                        self.upgrade_me = extra
                    else:
                        self.upgrade_me = [x for x in self.upgrade_me if x not in set(selected)]
            else:
                output_logger.info("FAILED\n")
                if not transaction:
                    # if we 'FAILED', but we cannot rollback, we will probably
                    # leave a broken state behind
                    # this should not happen
                    raise AssertionError("do_all FAILED but no transaction to rollback")
                transaction.rollback()
                if self.options.check_consistency_level >= 2:
                    target_suite.check_suite_source_pkg_consistency('do_all after rollback')

        output_logger.info("")

    def assert_nuninst_is_correct(self):
        self.logger.info("> Update complete - Verifying non-installability counters")

        cached_nuninst = self.nuninst_orig
        self._inst_tester.compute_installability()
        computed_nuninst = compile_nuninst(self.suite_info.target_suite,
                                           self.options.architectures,
                                           self.options.nobreakall_arches)
        if cached_nuninst != computed_nuninst:  # pragma: no cover
            only_on_break_archs = True
            self.logger.error("==================== NUNINST OUT OF SYNC =========================")
            for arch in self.options.architectures:
                expected_nuninst = set(cached_nuninst[arch])
                actual_nuninst = set(computed_nuninst[arch])
                false_negatives = actual_nuninst - expected_nuninst
                false_positives = expected_nuninst - actual_nuninst
                # Britney does not quite work correctly with
                # break/fucked arches, so ignore issues there for now.
                if (false_negatives or false_positives) and arch not in self.options.break_arches:
                    only_on_break_archs = False
                if false_negatives:
                    self.logger.error(" %s - unnoticed nuninst: %s", arch, str(false_negatives))
                if false_positives:
                    self.logger.error(" %s - invalid nuninst: %s", arch, str(false_positives))
                self.logger.info(" %s - actual nuninst: %s", arch, str(actual_nuninst))
                self.logger.error("==================== NUNINST OUT OF SYNC =========================")
            if not only_on_break_archs:
                raise AssertionError("NUNINST OUT OF SYNC")
            else:
                self.logger.warning("Nuninst is out of sync on some break arches")

        self.logger.info("> All non-installability counters are ok")

    def upgrade_testing(self):
        """Upgrade testing using the packages from the source suites

        This method tries to upgrade testing using the packages from the
        source suites.
        Before running the do_all method, it tries the easy and force-hint
        commands.
        """

        output_logger = self.output_logger
        self.logger.info("Starting the upgrade test")
        output_logger.info("Generated on: %s", time.strftime("%Y.%m.%d %H:%M:%S %z", time.gmtime(time.time())))
        output_logger.info("Arch order is: %s", ", ".join(self.options.architectures))

        if not self.options.actions:
            # process `easy' hints
            for x in self.hints['easy']:
                self.do_hint("easy", x.user, x.packages)

            # process `force-hint' hints
            for x in self.hints["force-hint"]:
                self.do_hint("force-hint", x.user, x.packages)

        # run the first round of the upgrade
        # - do separate runs for break arches
        allpackages = []
        normpackages = self.upgrade_me[:]
        archpackages = {}
        for a in self.options.break_arches:
            archpackages[a] = [p for p in normpackages if p.architecture == a]
            normpackages = [p for p in normpackages if p not in archpackages[a]]
        self.upgrade_me = normpackages
        output_logger.info("info: main run")
        self.do_all()
        allpackages += self.upgrade_me
        for a in self.options.break_arches:
            backup = self.options.break_arches
            self.options.break_arches = " ".join(x for x in self.options.break_arches if x != a)
            self.upgrade_me = archpackages[a]
            output_logger.info("info: broken arch run for %s", a)
            self.do_all()
            allpackages += self.upgrade_me
            self.options.break_arches = backup
        self.upgrade_me = allpackages

        if self.options.actions:
            self.printuninstchange()
            return

        # process `hint' hints
        hintcnt = 0
        for x in self.hints["hint"][:50]:
            if hintcnt > 50:
                output_logger.info("Skipping remaining hints...")
                break
            if self.do_hint("hint", x.user, x.packages):
                hintcnt += 1

        # run the auto hinter
        self.run_auto_hinter()

        if getattr(self.options, "remove_obsolete", "yes") == "yes":
            # obsolete source packages
            # a package is obsolete if none of the binary packages in testing
            # are built by it
            self.logger.info("> Removing obsolete source packages from the target suite")
            # local copies for performance
            target_suite = self.suite_info.target_suite
            sources_t = target_suite.sources
            binaries_t = target_suite.binaries
            mi_factory = self._migration_item_factory
            used = set(binaries_t[arch][binary].source
                       for arch in binaries_t
                       for binary in binaries_t[arch]
                       )
            removals = [mi_factory.parse_item("-%s/%s" % (source, sources_t[source].version), auto_correct=False)
                        for source in sources_t if source not in used
                        ]
            if removals:
                output_logger.info("Removing obsolete source packages from the target suite (%d):", len(removals))
                self.do_all(actions=removals)

        # smooth updates
        removals = old_libraries(self._migration_item_factory, self.suite_info, self.options.outofsync_arches)
        if removals:
            output_logger.info("Removing packages left in the target suite (e.g. smooth updates or cruft)")
            log_and_format_old_libraries(self.output_logger, removals)
            self.do_all(actions=removals, break_ok=True)
            removals = old_libraries(self._migration_item_factory, self.suite_info, self.options.outofsync_arches)

        output_logger.info("List of old libraries in the target suite (%d):", len(removals))
        log_and_format_old_libraries(self.output_logger, removals)

        self.printuninstchange()
        if self.options.check_consistency_level >= 1:
            target_suite = self.suite_info.target_suite
            self.assert_nuninst_is_correct()
            target_suite.check_suite_source_pkg_consistency('end')

        # output files
        if not self.options.dry_run:
            target_suite = self.suite_info.target_suite

            if hasattr(self.options, 'heidi_output') and \
               self.options.heidi_output:
                # write HeidiResult
                self.logger.info("Writing Heidi results to %s", self.options.heidi_output)
                write_heidi(self.options.heidi_output,
                            target_suite,
                            outofsync_arches=self.options.outofsync_arches)

                self.logger.info("Writing delta to %s", self.options.heidi_delta_output)
                write_heidi_delta(self.options.heidi_delta_output,
                                  self.all_selected)

        self.logger.info("Test completed!")

    def printuninstchange(self):
        self.logger.info("Checking for newly uninstallable packages")
        uninst = newly_uninst(self.nuninst_orig_save, self.nuninst_orig)

        if uninst:
            self.output_logger.warning("")
            self.output_logger.warning("Newly uninstallable packages in the target suite:")
            format_and_log_uninst(self.output_logger,
                                  self.options.architectures,
                                  uninst,
                                  loglevel=logging.WARNING,
                                  )

    def hint_tester(self):
        """Run a command line interface to test hints

        This method provides a command line interface for the release team to
        try hints and evaluate the results.
        """
        import readline
        from britney2.completer import Completer

        histfile = os.path.expanduser('~/.britney2_history')
        if os.path.exists(histfile):
            readline.read_history_file(histfile)

        readline.parse_and_bind('tab: complete')
        readline.set_completer(Completer(self).completer)
        # Package names can contain "-" and we use "/" in our presentation of them as well,
        # so ensure readline does not split on these characters.
        readline.set_completer_delims(readline.get_completer_delims().replace('-', '').replace('/', ''))

        known_hints = self._hint_parser.registered_hints

        print("Britney hint tester")
        print()
        print("Besides inputting known britney hints, the follow commands are also available")
        print(" * quit/exit      - terminates the shell")
        print(" * python-console - jump into an interactive python shell (with the current loaded dataset)")
        print()

        while True:
            # read the command from the command line
            try:
                user_input = input('britney> ').split()
            except EOFError:
                print("")
                break
            except KeyboardInterrupt:
                print("")
                continue
            # quit the hint tester
            if user_input and user_input[0] in ('quit', 'exit'):
                break
            elif user_input and user_input[0] == 'python-console':
                try:
                    import britney2.console
                except ImportError as e:
                    print("Failed to import britney.console module: %s" % repr(e))
                    continue
                britney2.console.run_python_console(self)
                print("Returning to the britney hint-tester console")
                # run a hint
            elif user_input and user_input[0] in ('easy', 'hint', 'force-hint'):
                mi_factory = self._migration_item_factory
                try:
                    self.do_hint(user_input[0], 'hint-tester', mi_factory.parse_items(user_input[1:]))
                    self.printuninstchange()
                except KeyboardInterrupt:
                    continue
            elif user_input and user_input[0] in known_hints:
                self._hint_parser.parse_hints('hint-tester', self.HINTS_ALL, '<stdin>', [' '.join(user_input)])
                self.write_excuses()

        try:
            readline.write_history_file(histfile)
        except IOError as e:
            self.logger.warning("Could not write %s: %s", histfile, e)

    def do_hint(self, hinttype, who, pkgvers):
        """Process hints

        This method process `easy`, `hint` and `force-hint` hints. If the
        requested version is not in the relevant source suite, then the hint
        is skipped.
        """

        output_logger = self.output_logger

        self.logger.info("> Processing '%s' hint from %s", hinttype, who)
        output_logger.info("Trying %s from %s: %s", hinttype, who,
                           " ".join("%s/%s" % (x.uvname, x.version) for x in pkgvers)
                           )

        issues = []
        # loop on the requested packages and versions
        for idx in range(len(pkgvers)):
            pkg = pkgvers[idx]
            # skip removal requests
            if pkg.is_removal:
                continue

            suite = pkg.suite

            if pkg.package not in suite.sources:
                issues.append("Source %s has no version in %s" % (pkg.package, suite.name))
            elif apt_pkg.version_compare(suite.sources[pkg.package].version, pkg.version) != 0:
                issues.append("Version mismatch, %s %s != %s" % (pkg.package, pkg.version,
                                                                 suite.sources[pkg.package].version))
        if issues:
            output_logger.warning("%s: Not using hint", ", ".join(issues))
            return False

        self.do_all(hinttype, pkgvers)
        return True

    def get_auto_hinter_hints(self, upgrade_me):
        """Auto-generate "easy" hints.

        This method attempts to generate "easy" hints for sets of packages which
        must migrate together. Beginning with a package which does not depend on
        any other package (in terms of excuses), a list of dependencies and
        reverse dependencies is recursively created.

        Once all such lists have been generated, any which are subsets of other
        lists are ignored in favour of the larger lists. The remaining lists are
        then attempted in turn as "easy" hints.

        We also try to auto hint circular dependencies analyzing the update
        excuses relationships. If they build a circular dependency, which we already
        know as not-working with the standard do_all algorithm, try to `easy` them.
        """
        self.logger.info("> Processing hints from the auto hinter")

        self.logger.info(">> Skipping auto hinting due to recursion problem")
        return []

        sources_t = self.suite_info.target_suite.sources
        excuses = self.excuses

        def excuse_still_valid(excuse):
            source = excuse.source
            arch = excuse.item.architecture
            # TODO for binNMUs, this check is always ok, even if the item
            # migrated already
            valid = (arch != 'source' or
                     source not in sources_t or
                     sources_t[source].version != excuse.ver[1])
            # TODO migrated items should be removed from upgrade_me, so this
            # should not happen
            if not valid:
                raise AssertionError("excuse no longer valid %s" % (item))
            return valid

        # consider only excuses which are valid candidates and still relevant.
        valid_excuses = frozenset(e.name for n, e in excuses.items()
                                  if e.item in upgrade_me
                                  and excuse_still_valid(e))
        excuses_deps = {name: valid_excuses.intersection(excuse.get_deps())
                        for name, excuse in excuses.items() if name in valid_excuses}
        excuses_rdeps = defaultdict(set)
        for name, deps in excuses_deps.items():
            for dep in deps:
                excuses_rdeps[dep].add(name)

        def find_related(e, hint, circular_first=False):
            excuse = excuses[e]
            if not circular_first:
                hint.add(excuse.item)
            if not excuse.get_deps():
                return hint
            for p in excuses_deps[e]:
                if p in hint or p not in valid_excuses:
                    continue
                if not find_related(p, hint):
                    return False
            return hint

        # loop on them
        candidates = []
        mincands = []
        seen_hints = set()
        for e in valid_excuses:
            excuse = excuses[e]
            if excuse.get_deps():
                hint = find_related(e, set(), True)
                if isinstance(hint, dict) and e in hint:
                    h = frozenset(hint)
                    if h not in seen_hints:
                        candidates.append(h)
                        seen_hints.add(h)
            else:
                items = [excuse.item]
                orig_size = 1
                looped = False
                seen_items = set()
                seen_items.update(items)

                for item in items:
                    # excuses which depend on "item" or are depended on by it
                    new_items = {excuses[x].item for x in chain(excuses_deps[item.name], excuses_rdeps[item.name])}
                    new_items -= seen_items
                    items.extend(new_items)
                    seen_items.update(new_items)

                    if not looped and len(items) > 1:
                        orig_size = len(items)
                        h = frozenset(seen_items)
                        if h not in seen_hints:
                            mincands.append(h)
                            seen_hints.add(h)
                    looped = True
                if len(items) != orig_size:
                    h = frozenset(seen_items)
                    if h != mincands[-1] and h not in seen_hints:
                        candidates.append(h)
                        seen_hints.add(h)
        return [candidates, mincands]

    def run_auto_hinter(self):
        mi_factory = self._migration_item_factory
        for l in self.get_auto_hinter_hints(self.upgrade_me):
            for hint in l:
                self.do_hint("easy", "autohinter", sorted(hint))

    def nuninst_arch_report(self, nuninst, arch):
        """Print a report of uninstallable packages for one architecture."""
        all = defaultdict(set)
        binaries_t = self.suite_info.target_suite.binaries
        for p in nuninst[arch]:
            pkg = binaries_t[arch][p]
            all[(pkg.source, pkg.source_version)].add(p)

        print('* %s' % arch)

        for (src, ver), pkgs in sorted(all.items()):
            print('  %s (%s): %s' % (src, ver, ' '.join(sorted(pkgs))))

        print()

    def main(self):
        """Main method

        This is the entry point for the class: it includes the list of calls
        for the member methods which will produce the output files.
        """
        # if running in --print-uninst mode, quit
        if self.options.print_uninst:
            return
        # if no actions are provided, build the excuses and sort them
        elif not self.options.actions:
            self.write_excuses()
        # otherwise, use the actions provided by the command line
        else:
            self.upgrade_me = self.options.actions.split()

        if self.options.compute_migrations or self.options.hint_tester:
            if self.options.dry_run:
                self.logger.info("Upgrade output not (also) written to a separate file"
                                 " as this is a dry-run.")
            elif hasattr(self.options, 'upgrade_output'):
                upgrade_output = getattr(self.options, 'upgrade_output')
                os.makedirs(os.path.dirname(self.options.upgrade_output), exist_ok=True)
                file_handler = logging.FileHandler(upgrade_output, mode='w', encoding='utf-8')
                output_formatter = logging.Formatter('%(message)s')
                file_handler.setFormatter(output_formatter)
                self.output_logger.addHandler(file_handler)
                self.logger.info("Logging upgrade output to %s", upgrade_output)
            else:
                self.logger.info("Upgrade output not (also) written to a separate file"
                                 " as the UPGRADE_OUTPUT configuration is not provided.")

            # run the hint tester
            if self.options.hint_tester:
                self.hint_tester()
            # run the upgrade test
            else:
                self.upgrade_testing()

            self.logger.info('> Stats from the installability tester')
            for stat in self._inst_tester.stats.stats():
                self.logger.info('>   %s', stat)
        else:
            self.logger.info('Migration computation skipped as requested.')
        if not self.options.dry_run:
            self._policy_engine.save_state(self)
        logging.shutdown()


if __name__ == '__main__':
    Britney().main()
