from itertools import chain
from urllib.parse import quote

import apt_pkg
import logging

from britney2 import DependencyType, PackageId
from britney2.excuse import Excuse
from britney2.excusedeps import DependencySpec
from britney2.migrationitem import MigrationItem
from britney2.policies import PolicyVerdict
from britney2.utils import (invalidate_excuses, find_smooth_updateable_binaries,
                            get_dependency_solvers,
                            )


class ExcuseFinder(object):

    def __init__(self, options, suite_info, all_binaries, pkg_universe, policy_engine, mi_factory, hints):
        logger_name = ".".join((self.__class__.__module__, self.__class__.__name__))
        self.logger = logging.getLogger(logger_name)
        self.options = options
        self.suite_info = suite_info
        self.all_binaries = all_binaries
        self.pkg_universe = pkg_universe
        self._policy_engine = policy_engine
        self._migration_item_factory = mi_factory
        self.hints = hints
        self.excuses = {}

    def _should_remove_source(self, item):
        """Check if a source package should be removed from testing

        This method checks if a source package should be removed from the
        target suite; this happens if the source package is not
        present in the primary source suite anymore.

        It returns True if the package can be removed, False otherwise.
        In the former case, a new excuse is appended to the object
        attribute excuses.
        """
        if hasattr(self.options, 'partial_source'):
            return False
        # if the source package is available in unstable, then do nothing
        source_suite = self.suite_info.primary_source_suite
        pkg = item.package
        if pkg in source_suite.sources:
            return False
        # otherwise, add a new excuse for its removal
        src = item.suite.sources[pkg]
        excuse = Excuse(item)
        excuse.addinfo("Package not in %s, will try to remove" % source_suite.name)
        excuse.set_vers(src.version, None)
        src.maintainer and excuse.set_maint(src.maintainer)
        src.section and excuse.set_section(src.section)

        # if the package is blocked, skip it
        for hint in self.hints.search('block', package=pkg, removal=True):
            excuse.policy_verdict = PolicyVerdict.REJECTED_PERMANENTLY
            excuse.add_verdict_info(
                excuse.policy_verdict,
                "Not touching package, as requested by %s "
                "(contact debian-release if update is needed)" % hint.user)
            excuse.addreason("block")
            self.excuses[excuse.name] = excuse
            return False

        excuse.policy_verdict = PolicyVerdict.PASS
        self.excuses[excuse.name] = excuse
        return True

    def _should_upgrade_srcarch(self, item):
        """Check if a set of binary packages should be upgraded

        This method checks if the binary packages produced by the source
        package on the given architecture should be upgraded; this can
        happen also if the migration is a binary-NMU for the given arch.

        It returns False if the given packages don't need to be upgraded,
        True otherwise. In the former case, a new excuse is appended to
        the object attribute excuses.
        """
        # retrieve the source packages for testing and suite

        target_suite = self.suite_info.target_suite
        source_suite = item.suite
        src = item.package
        arch = item.architecture
        source_t = target_suite.sources[src]
        source_u = source_suite.sources[src]

        excuse = Excuse(item)
        excuse.set_vers(source_t.version, source_t.version)
        source_u.maintainer and excuse.set_maint(source_u.maintainer)
        source_u.section and excuse.set_section(source_u.section)

        # if there is a `remove' hint and the requested version is the same as the
        # version in testing, then stop here and return False
        # (as a side effect, a removal may generate such excuses for both the source
        # package and its binary packages on each architecture)
        for hint in self.hints.search('remove', package=src, version=source_t.version):
            excuse.add_hint(hint)
            excuse.policy_verdict = PolicyVerdict.REJECTED_PERMANENTLY
            excuse.add_verdict_info(excuse.policy_verdict, "Removal request by %s" % (hint.user))
            excuse.add_verdict_info(excuse.policy_verdict, "Trying to remove package, not update it")
            self.excuses[excuse.name] = excuse
            return False

        # the starting point is that there is nothing wrong and nothing worth doing
        anywrongver = False
        anyworthdoing = False

        packages_t_a = target_suite.binaries[arch]
        packages_s_a = source_suite.binaries[arch]

        wrong_verdict = PolicyVerdict.REJECTED_PERMANENTLY

        # for every binary package produced by this source in unstable for this architecture
        for pkg_id in sorted(x for x in source_u.binaries if x.architecture == arch):
            pkg_name = pkg_id.package_name
            # TODO filter binaries based on checks below?
            excuse.add_package(pkg_id)

            # retrieve the testing (if present) and unstable corresponding binary packages
            binary_t = packages_t_a[pkg_name] if pkg_name in packages_t_a else None
            binary_u = packages_s_a[pkg_name]

            # this is the source version for the new binary package
            pkgsv = binary_u.source_version

            # if the new binary package is architecture-independent, then skip it
            if binary_u.architecture == 'all':
                if pkg_id not in source_t.binaries:
                    # only add a note if the arch:all does not match the expected version
                    excuse.add_detailed_info("Ignoring %s %s (from %s) as it is arch: all" % (pkg_name, binary_u.version, pkgsv))
                continue

            # if the new binary package is not from the same source as the testing one, then skip it
            # this implies that this binary migration is part of a source migration
            if source_u.version == pkgsv and source_t.version != pkgsv:
                anywrongver = True
                excuse.add_verdict_info(
                    wrong_verdict,
                    "From wrong source: %s %s (%s not %s)" %
                    (pkg_name, binary_u.version, pkgsv, source_t.version))
                continue

            # cruft in unstable
            if source_u.version != pkgsv and source_t.version != pkgsv:
                if self.options.ignore_cruft:
                    excuse.add_detailed_info("Old cruft: %s %s (but ignoring cruft, so nevermind)" % (pkg_name, pkgsv))
                else:
                    anywrongver = True
                    excuse.add_verdict_info(wrong_verdict, "Old cruft: %s %s" % (pkg_name, pkgsv))
                continue

            # if the source package has been updated in unstable and this is a binary migration, skip it
            # (the binaries are now out-of-date)
            if source_t.version == pkgsv and source_t.version != source_u.version:
                anywrongver = True
                excuse.add_verdict_info(
                    wrong_verdict,
                    "From wrong source: %s %s (%s not %s)" %
                    (pkg_name, binary_u.version, pkgsv, source_u.version))
                continue

            # if the binary is not present in testing, then it is a new binary;
            # in this case, there is something worth doing
            if not binary_t:
                excuse.add_detailed_info("New binary: %s (%s)" % (pkg_name, binary_u.version))
                anyworthdoing = True
                continue

            # at this point, the binary package is present in testing, so we can compare
            # the versions of the packages ...
            vcompare = apt_pkg.version_compare(binary_t.version, binary_u.version)

            # ... if updating would mean downgrading, then stop here: there is something wrong
            if vcompare > 0:
                anywrongver = True
                excuse.add_verdict_info(
                    wrong_verdict,
                    "Not downgrading: %s (%s to %s)" % (pkg_name, binary_t.version, binary_u.version))
                break
            # ... if updating would mean upgrading, then there is something worth doing
            elif vcompare < 0:
                excuse.add_detailed_info("Updated binary: %s (%s to %s)" % (pkg_name, binary_t.version, binary_u.version))
                anyworthdoing = True

        srcv = source_u.version
        same_source = source_t.version == srcv
        primary_source_suite = self.suite_info.primary_source_suite
        is_primary_source = source_suite == primary_source_suite

        # if there is nothing wrong and there is something worth doing or the source
        # package is not fake, then check what packages should be removed
        if not anywrongver and (anyworthdoing or not source_u.is_fakesrc):
            # we want to remove binaries that are no longer produced by the
            # new source, but there are some special cases:
            # - if this is binary-only (same_source) and not from the primary
            #   source, we don't do any removals:
            #   binNMUs in *pu on some architectures would otherwise result in
            #   the removal of binaries on other architectures
            # - for the primary source, smooth binaries in the target suite
            #   are not considered for removal
            if not same_source or is_primary_source:
                smoothbins = set()
                if is_primary_source:
                    binaries_t = target_suite.binaries
                    possible_smooth_updates = [p for p in source_t.binaries if p.architecture == arch]
                    smoothbins = find_smooth_updateable_binaries(possible_smooth_updates,
                                                                 source_u,
                                                                 self.pkg_universe,
                                                                 target_suite,
                                                                 binaries_t,
                                                                 source_suite.binaries,
                                                                 frozenset(),
                                                                 self.options.smooth_updates,
                                                                 self.hints)

                # for every binary package produced by this source in testing for this architecture
                for pkg_id in sorted(x for x in source_t.binaries if x.architecture == arch):
                    pkg = pkg_id.package_name
                    # if the package is architecture-independent, then ignore it
                    tpkg_data = packages_t_a[pkg]
                    if tpkg_data.architecture == 'all':
                        if pkg_id not in source_u.binaries:
                            # only add a note if the arch:all does not match the expected version
                            excuse.add_detailed_info("Ignoring removal of %s as it is arch: all" % (pkg))
                        continue
                    # if the package is not produced by the new source package, then remove it from testing
                    if pkg not in packages_s_a:
                        excuse.add_detailed_info("Removed binary: %s %s" % (pkg, tpkg_data.version))
                        # the removed binary is only interesting if this is a binary-only migration,
                        # as otherwise the updated source will already cause the binary packages
                        # to be updated
                        if same_source and pkg_id not in smoothbins:
                            # Special-case, if the binary is a candidate for a smooth update, we do not consider
                            # it "interesting" on its own.  This case happens quite often with smooth updatable
                            # packages, where the old binary "survives" a full run because it still has
                            # reverse dependencies.
                            anyworthdoing = True

        if not anyworthdoing:
            # nothing worth doing, we don't add an excuse to the list, we just return false
            return False

        # there is something worth doing
        # we assume that this package will be ok, if not invalidated below
        excuse.policy_verdict = PolicyVerdict.PASS

        # if there is something something wrong, reject this package
        if anywrongver:
            excuse.policy_verdict = wrong_verdict

        self._policy_engine.apply_srcarch_policies(item, arch, source_t, source_u, excuse)

        self.excuses[excuse.name] = excuse
        return excuse.is_valid

    def _should_upgrade_src(self, item):
        """Check if source package should be upgraded

        This method checks if a source package should be upgraded. The analysis
        is performed for the source package specified by the `src' parameter,
        for the distribution `source_suite'.

        It returns False if the given package doesn't need to be upgraded,
        True otherwise. In the former case, a new excuse is appended to
        the object attribute excuses.
        """

        src = item.package
        source_suite = item.suite
        suite_name = source_suite.name
        source_u = source_suite.sources[src]
        if source_u.is_fakesrc:
            # it is a fake package created to satisfy Britney implementation details; silently ignore it
            return False

        target_suite = self.suite_info.target_suite
        # retrieve the source packages for testing (if available) and suite
        if src in target_suite.sources:
            source_t = target_suite.sources[src]
            # if testing and unstable have the same version, then this is a candidate for binary-NMUs only
            if apt_pkg.version_compare(source_t.version, source_u.version) == 0:
                return False
        else:
            source_t = None

        excuse = Excuse(item)
        excuse.set_vers(source_t and source_t.version or None, source_u.version)
        source_u.maintainer and excuse.set_maint(source_u.maintainer)
        source_u.section and excuse.set_section(source_u.section)
        excuse.add_package(PackageId(src, source_u.version, "source"))

        # if the version in unstable is older, then stop here with a warning in the excuse and return False
        if source_t and apt_pkg.version_compare(source_u.version, source_t.version) < 0:
            excuse.policy_verdict = PolicyVerdict.REJECTED_PERMANENTLY
            excuse.add_verdict_info(
                excuse.policy_verdict,
                "ALERT: %s is newer in the target suite (%s %s)" % (src, source_t.version, source_u.version))
            self.excuses[excuse.name] = excuse
            excuse.addreason("newerintesting")
            return False

        # the starting point is that we will update the candidate
        excuse.policy_verdict = PolicyVerdict.PASS

        # if there is a `remove' hint and the requested version is the same as the
        # version in testing, then stop here and return False
        for hint in self.hints.search('remove', package=src):
            if source_t and source_t.version == hint.version or \
                    source_u.version == hint.version:
                excuse.add_hint(hint)
                excuse.policy_verdict = PolicyVerdict.REJECTED_PERMANENTLY
                excuse.add_verdict_info(excuse.policy_verdict, "Removal request by %s" % (hint.user))
                excuse.add_verdict_info(excuse.policy_verdict, "Trying to remove package, not update it")
                break

        all_binaries = self.all_binaries

        # at this point, we check the status of the builds on all the supported architectures
        # to catch the out-of-date ones
        archs_to_consider = list(self.options.architectures)
        archs_to_consider.append('all')
        for arch in archs_to_consider:
            oodbins = {}
            uptodatebins = False
            # for every binary package produced by this source in the suite for this architecture
            if arch == 'all':
                consider_binaries = source_u.binaries
            else:
                # Will also include arch:all for the given architecture (they are filtered out
                # below)
                consider_binaries = sorted(x for x in source_u.binaries if x.architecture == arch)
            for pkg_id in consider_binaries:
                pkg = pkg_id.package_name

                # retrieve the binary package and its source version
                binary_u = all_binaries[pkg_id]
                pkgsv = binary_u.source_version

                # arch:all packages are treated separately from arch:arch
                if binary_u.architecture != arch:
                    continue

                # TODO filter binaries based on checks below?
                excuse.add_package(pkg_id)

                # if it wasn't built by the same source, it is out-of-date
                # if there is at least one binary on this arch which is
                # up-to-date, there is a build on this arch
                if source_u.version != pkgsv:
                    if pkgsv not in oodbins:
                        oodbins[pkgsv] = set()
                    oodbins[pkgsv].add(pkg)
                    excuse.add_old_binary(pkg, pkgsv)
                    continue
                else:
                    uptodatebins = True

            # if there are out-of-date packages, warn about them in the excuse and set excuse.is_valid
            # to False to block the update; if the architecture where the package is out-of-date is
            # in the `outofsync_arches' list, then do not block the update
            if oodbins:
                oodtxt = ""
                for v in sorted(oodbins):
                    if oodtxt:
                        oodtxt = oodtxt + "; "
                    oodtxt = oodtxt + "%s (from <a href=\"https://buildd.debian.org/status/logs.php?" \
                                      "arch=%s&pkg=%s&ver=%s\" target=\"_blank\">%s</a>)" % \
                                      (", ".join(sorted(oodbins[v])), quote(arch), quote(src), quote(v), v)
                if uptodatebins:
                    text = "old binaries left on <a href=\"https://buildd.debian.org/status/logs.php?" \
                           "arch=%s&pkg=%s&ver=%s\" target=\"_blank\">%s</a>: %s" % \
                           (quote(arch), quote(src), quote(source_u.version), arch, oodtxt)
                else:
                    text = "missing build on <a href=\"https://buildd.debian.org/status/logs.php?" \
                           "arch=%s&pkg=%s&ver=%s\" target=\"_blank\">%s</a>" % \
                           (quote(arch), quote(src), quote(source_u.version), arch)

                if arch in self.options.outofsync_arches:
                    text = text + " (but %s isn't keeping up, so nevermind)" % (arch)
                    if not uptodatebins:
                        excuse.missing_build_on_ood_arch(arch)
                else:
                    if uptodatebins:
                        if self.options.ignore_cruft:
                            text = text + " (but ignoring cruft, so nevermind)"
                            excuse.add_detailed_info(text)
                        else:
                            excuse.policy_verdict = PolicyVerdict.REJECTED_PERMANENTLY
                            excuse.addreason("cruft")
                            excuse.add_verdict_info(excuse.policy_verdict, text)
                    else:
                        excuse.policy_verdict = PolicyVerdict.REJECTED_CANNOT_DETERMINE_IF_PERMANENT
                        excuse.missing_build_on_arch(arch)
                        excuse.addreason("missingbuild")
                        excuse.add_verdict_info(excuse.policy_verdict, text)
                        excuse.add_detailed_info("old binaries on %s: %s" % (arch, oodtxt))

        # if the source package has no binaries, set is_valid to False to block the update
        if not source_u.binaries:
            excuse.policy_verdict = PolicyVerdict.REJECTED_PERMANENTLY
            excuse.add_verdict_info(excuse.policy_verdict, "%s has no binaries on any arch" % src)
            excuse.addreason("no-binaries")

        self._policy_engine.apply_src_policies(item, source_t, source_u, excuse)

        if source_suite.suite_class.is_additional_source and source_t:
            # o-o-d(ish) checks for (t-)p-u
            # This only makes sense if the package is actually in testing.
            for arch in self.options.architectures:
                # if the package in testing has no binaries on this
                # architecture, it can't be out-of-date
                if not any(x for x in source_t.binaries
                           if x.architecture == arch and all_binaries[x].architecture != 'all'):
                    continue

                # if the (t-)p-u package has produced any binaries on
                # this architecture then we assume it's ok. this allows for
                # uploads to (t-)p-u which intentionally drop binary
                # packages
                if any(x for x in source_suite.binaries[arch].values()
                       if x.source == src and x.source_version == source_u.version and x.architecture != 'all'):
                    continue

                # TODO: Find a way to avoid hardcoding pu/stable relation.
                if suite_name == 'pu':
                    base = 'stable'
                else:
                    base = target_suite.name
                text = "Not yet built on "\
                    "<a href=\"https://buildd.debian.org/status/logs.php?"\
                    "arch=%s&pkg=%s&ver=%s&suite=%s\" target=\"_blank\">%s</a> "\
                    "(relative to target suite)" % \
                    (quote(arch), quote(src), quote(source_u.version), base, arch)

                if arch in self.options.outofsync_arches:
                    text = text + " (but %s isn't keeping up, so never mind)" % (arch)
                    excuse.missing_build_on_ood_arch(arch)
                    excuse.addinfo(text)
                else:
                    excuse.policy_verdict = PolicyVerdict.REJECTED_CANNOT_DETERMINE_IF_PERMANENT
                    excuse.missing_build_on_arch(arch)
                    excuse.addreason("missingbuild")
                    excuse.add_verdict_info(excuse.policy_verdict, text)

        # check if there is a `force' hint for this package, which allows it to go in even if it is not updateable
        forces = self.hints.search('force', package=src, version=source_u.version)
        if forces:
            # force() updates the final verdict for us
            changed_state = excuse.force()
            if changed_state:
                excuse.addinfo("Should ignore, but forced by %s" % (forces[0].user))

        self.excuses[excuse.name] = excuse
        return excuse.is_valid

    def _compute_excuses_and_initial_actionable_items(self):
        # list of local methods and variables (for better performance)
        excuses = self.excuses
        suite_info = self.suite_info
        pri_source_suite = suite_info.primary_source_suite
        architectures = self.options.architectures
        should_remove_source = self._should_remove_source
        should_upgrade_srcarch = self._should_upgrade_srcarch
        should_upgrade_src = self._should_upgrade_src
        mi_factory = self._migration_item_factory

        sources_ps = pri_source_suite.sources
        sources_t = suite_info.target_suite.sources

        # this set will contain the packages which are valid candidates;
        # if a package is going to be removed, it will have a "-" prefix
        actionable_items = set()
        actionable_items_add = actionable_items.add  # Every . in a loop slows it down

        # for every source package in testing, check if it should be removed
        for pkg in sources_t:
            if pkg not in sources_ps:
                src = sources_t[pkg]
                item = MigrationItem(package=pkg,
                                     version=src.version,
                                     suite=suite_info.target_suite,
                                     is_removal=True)
                if should_remove_source(item):
                    actionable_items_add(item)

        # for every source package in the source suites, check if it should be upgraded
        for suite in chain((pri_source_suite, *suite_info.additional_source_suites)):
            sources_s = suite.sources
            item_suffix = "_%s" % suite.excuses_suffix if suite.excuses_suffix else ''
            for pkg in sources_s:
                src_s_data = sources_s[pkg]
                if src_s_data.is_fakesrc:
                    continue
                src_t_data = sources_t.get(pkg)

                if src_t_data is None or apt_pkg.version_compare(src_s_data.version, src_t_data.version) != 0:
                    item = MigrationItem(package=pkg,
                                         version=src_s_data.version,
                                         suite=suite)
                    # check if the source package should be upgraded
                    if should_upgrade_src(item):
                        actionable_items_add(item)
                else:
                    # package has same version in source and target suite; check if any of the
                    # binaries have changed on the various architectures
                    for arch in architectures:
                        item = MigrationItem(package=pkg,
                                             version=src_s_data.version,
                                             architecture=arch,
                                             suite=suite)
                        if should_upgrade_srcarch(item):
                            actionable_items_add(item)

        # process the `remove' hints, if the given package is not yet in actionable_items
        for hint in self.hints['remove']:
            src = hint.package
            if src not in sources_t:
                continue

            existing_items = set(x for x in actionable_items if x.package == src)
            if existing_items:
                self.logger.info("removal hint '%s' ignored due to existing item(s) %s" %
                                 (hint, [i.name for i in existing_items]))
                continue

            tsrcv = sources_t[src].version
            item = MigrationItem(package=src,
                                 version=tsrcv,
                                 suite=suite_info.target_suite,
                                 is_removal=True)

            # check if the version specified in the hint is the same as the considered package
            if tsrcv != hint.version:
                continue

            # add the removal of the package to actionable_items and build a new excuse
            excuse = Excuse(item)
            excuse.set_vers(tsrcv, None)
            excuse.addinfo("Removal request by %s" % (hint.user))
            # if the removal of the package is blocked, skip it
            blocked = False
            for blockhint in self.hints.search('block', package=src, removal=True):
                excuse.policy_verdict = PolicyVerdict.REJECTED_PERMANENTLY
                excuse.add_verdict_info(
                    excuse.policy_verdict,
                    "Not removing package, due to block hint by %s "
                    "(contact debian-release if update is needed)" % blockhint.user)
                excuse.addreason("block")
                blocked = True

            if blocked:
                excuses[excuse.name] = excuse
                continue

            actionable_items_add(item)
            excuse.addinfo("Package is broken, will try to remove")
            excuse.add_hint(hint)
            # Using "PASS" here as "Created by a hint" != "accepted due to hint".  In a future
            # where there might be policy checks on removals, it would make sense to distinguish
            # those two states.  Not sure that future will ever be.
            excuse.policy_verdict = PolicyVerdict.PASS
            excuses[excuse.name] = excuse

        return actionable_items

    def find_actionable_excuses(self):
        excuses = self.excuses
        actionable_items = self._compute_excuses_and_initial_actionable_items()
        valid = {x.name for x in actionable_items}

        # extract the not considered packages, which are in the excuses but not in upgrade_me
        unconsidered = {ename for ename in excuses if ename not in valid}
        invalidated = set()

        invalidate_excuses(excuses, valid, unconsidered, invalidated)

        # check that the list of actionable items matches the list of valid
        # excuses
        assert valid == {x for x in excuses if excuses[x].is_valid}

        # check that the rdeps for all invalid excuses were invalidated
        assert invalidated == {x for x in excuses if not excuses[x].is_valid}

        actionable_items = {x for x in actionable_items if x.name in valid}
        return excuses, actionable_items
