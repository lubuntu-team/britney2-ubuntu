# -*- coding: utf-8 -*-

# Refactored parts from britney.py, which is/was:
# Copyright (C) 2001-2008 Anthony Towns <ajt@debian.org>
#                         Andreas Barth <aba@debian.org>
#                         Fabio Tranchitella <kobold@debian.org>
# Copyright (C) 2010-2012 Adam D. Barratt <adsb@debian.org>
# Copyright (C) 2012 Niels Thykier <niels@thykier.net>
#
# New portions
# Copyright (C) 2013 Adam D. Barratt <adsb@debian.org>

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.


import apt_pkg
import errno
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from enum import Enum, unique
from functools import partial
from itertools import filterfalse, chain

import yaml

from britney2 import SourcePackage, SuiteClass, PackageId
from britney2.excusedeps import DependencySpec, ImpossibleDependencyState
from britney2.policies import PolicyVerdict


class MigrationConstraintException(Exception):
    pass


def ifilter_except(container, iterable=None):
    """Filter out elements in container

    If given an iterable it returns a filtered iterator, otherwise it
    returns a function to generate filtered iterators.  The latter is
    useful if the same filter has to be (re-)used on multiple
    iterators that are not known on beforehand.
    """
    if iterable is not None:
        return filterfalse(container.__contains__, iterable)
    return partial(filterfalse, container.__contains__)


def ifilter_only(container, iterable=None):
    """Filter out elements in which are not in container

    If given an iterable it returns a filtered iterator, otherwise it
    returns a function to generate filtered iterators.  The latter is
    useful if the same filter has to be (re-)used on multiple
    iterators that are not known on beforehand.
    """
    if iterable is not None:
        return filter(container.__contains__, iterable)
    return partial(filter, container.__contains__)


# iter_except is from the "itertools" recipe
def iter_except(func, exception, first=None):  # pragma: no cover - itertools recipe function
    """ Call a function repeatedly until an exception is raised.

    Converts a call-until-exception interface to an iterator interface.
    Like __builtin__.iter(func, sentinel) but uses an exception instead
    of a sentinel to end the loop.

    Examples:
        bsddbiter = iter_except(db.next, bsddb.error, db.first)
        heapiter = iter_except(functools.partial(heappop, h), IndexError)
        dictiter = iter_except(d.popitem, KeyError)
        dequeiter = iter_except(d.popleft, IndexError)
        queueiter = iter_except(q.get_nowait, Queue.Empty)
        setiter = iter_except(s.pop, KeyError)

    """
    try:
        if first is not None:
            yield first()
        while 1:
            yield func()
    except exception:
        pass


def log_and_format_old_libraries(logger, libs):
    """Format and log old libraries in a table (no header)"""
    libraries = {}
    for i in libs:
        pkg = i.package
        if pkg in libraries:
            libraries[pkg].append(i.architecture)
        else:
            libraries[pkg] = [i.architecture]

    for lib in sorted(libraries):
        logger.info(" %s: %s", lib, " ".join(libraries[lib]))


def compute_reverse_tree(pkg_universe, affected):
    """Calculate the full dependency tree for a set of packages

    This method returns the full dependency tree for a given set of
    packages.  The first argument is an instance of the BinaryPackageUniverse
    and the second argument are a set of BinaryPackageId.

    The set of affected packages will be updated in place and must
    therefore be mutable.
    """
    remain = list(affected)
    while remain:
        pkg_id = remain.pop()
        new_pkg_ids = pkg_universe.reverse_dependencies_of(pkg_id) - affected
        affected.update(new_pkg_ids)
        remain.extend(new_pkg_ids)
    return None


def add_transitive_dependencies_flatten(pkg_universe, initial_set):
    """Find and include all transitive dependencies

    This method updates the initial_set parameter to include all transitive
    dependencies. The first argument is an instance of the BinaryPackageUniverse
    and the second argument are a set of BinaryPackageId.

    The set of initial packages will be updated in place and must
    therefore be mutable.
    """
    remain = list(initial_set)
    while remain:
        pkg_id = remain.pop()
        new_pkg_ids = [x for x in chain.from_iterable(pkg_universe.dependencies_of(pkg_id)) if x not in initial_set]
        initial_set.update(new_pkg_ids)
        remain.extend(new_pkg_ids)
    return None


def write_nuninst(filename, nuninst):
    """Write the non-installable report

    Write the non-installable report derived from "nuninst" to the
    file denoted by "filename".
    """
    with open(filename, 'w', encoding='utf-8') as f:
        # Having two fields with (almost) identical dates seems a bit
        # redundant.
        f.write("Built on: " + time.strftime("%Y.%m.%d %H:%M:%S %z", time.gmtime(time.time())) + "\n")
        f.write("Last update: " + time.strftime("%Y.%m.%d %H:%M:%S %z", time.gmtime(time.time())) + "\n\n")
        for k in nuninst:
            f.write("%s: %s\n" % (k, " ".join(nuninst[k])))


def read_nuninst(filename, architectures):
    """Read the non-installable report

    Read the non-installable report from the file denoted by
    "filename" and return it.  Only architectures in "architectures"
    will be included in the report.
    """
    nuninst = {}
    with open(filename, encoding='ascii') as f:
        for r in f:
            if ":" not in r:
                continue
            arch, packages = r.strip().split(":", 1)
            if arch.split("+", 1)[0] in architectures:
                nuninst[arch] = set(packages.split())
    return nuninst


def newly_uninst(nuold, nunew):
    """Return a nuninst statstic with only new uninstallable packages

    This method subtracts the uninstallable packages of the statistic
    "nunew" from the statistic "nuold".

    It returns a dictionary with the architectures as keys and the list
    of uninstallable packages as values.  If there are no regressions
    on a given architecture, then the architecture will be omitted in
    the result.  Accordingly, if none of the architectures have
    regressions an empty directory is returned.
    """
    res = {}
    for arch in ifilter_only(nunew, nuold):
        arch_nuninst = [x for x in nunew[arch] if x not in nuold[arch]]
        # Leave res empty if there are no newly uninst packages
        if arch_nuninst:
            res[arch] = arch_nuninst
    return res


def format_and_log_uninst(logger, architectures, nuninst, *, loglevel=logging.INFO):
    """Emits the uninstallable packages to the log

    An example of the output string is:
      * i386: broken-pkg1, broken-pkg2

    Note that if there is no uninstallable packages, then nothing is emitted.
    """
    for arch in architectures:
        if arch in nuninst and nuninst[arch]:
            msg = "    * %s: %s" % (arch, ", ".join(sorted(nuninst[arch])))
            logger.log(loglevel, msg)


def write_heidi(filename, target_suite, *, outofsync_arches=frozenset(), sorted=sorted):
    """Write the output HeidiResult

    This method write the output for Heidi, which contains all the
    binary packages and the source packages in the form:

    <pkg-name> <pkg-version> <pkg-architecture> <pkg-section>
    <src-name> <src-version> source <src-section>

    The file is written as "filename" using the sources and packages
    from the "target_suite" parameter.

    outofsync_arches: If given, it is a set of architectures marked
    as "out of sync".  The output file may exclude some out of date
    arch:all packages for those architectures to reduce the noise.

    The "X=X" parameters are optimizations to avoid "load global" in
    the loops.
    """
    sources_t = target_suite.sources
    packages_t = target_suite.binaries

    with open(filename, 'w', encoding='ascii') as f:

        # write binary packages
        for arch in sorted(packages_t):
            binaries = packages_t[arch]
            for pkg_name in sorted(binaries):
                pkg = binaries[pkg_name]
                pkgv = pkg.version
                pkgarch = pkg.architecture or 'all'
                pkgsec = pkg.section or 'faux'
                if pkgsec == 'faux' or pkgsec.endswith('/faux'):
                    # Faux package; not really a part of testing
                    continue
                if pkg.source_version and pkgarch == 'all' and \
                        pkg.source_version != sources_t[pkg.source].version and \
                        arch in outofsync_arches:
                    # when architectures are marked as "outofsync", their binary
                    # versions may be lower than those of the associated
                    # source package in testing. the binary package list for
                    # such architectures will include arch:all packages
                    # matching those older versions, but we only want the
                    # newer arch:all in testing
                    continue
                f.write('%s %s %s %s\n' % (pkg_name, pkgv, pkgarch, pkgsec))

        # write sources
        for src_name in sorted(sources_t):
            src = sources_t[src_name]
            srcv = src.version
            srcsec = src.section or 'unknown'
            if srcsec == 'faux' or srcsec.endswith('/faux'):
                # Faux package; not really a part of testing
                continue
            f.write('%s %s source %s\n' % (src_name, srcv, srcsec))


def write_heidi_delta(filename, all_selected):
    """Write the output delta

    This method writes the packages to be upgraded, in the form:
    <src-name> <src-version>
    or (if the source is to be removed):
    -<src-name> <src-version>

    The order corresponds to that shown in update_output.
    """
    with open(filename, "w", encoding='ascii') as fd:

        fd.write("#HeidiDelta\n")

        for item in all_selected:
            prefix = ""

            if item.is_removal:
                prefix = "-"

            if item.architecture == 'source':
                fd.write('%s%s %s\n' % (prefix, item.package, item.version))
            else:
                fd.write('%s%s %s %s\n' % (prefix, item.package,
                                           item.version, item.architecture))


def write_excuses(excuses, dest_file, output_format="yaml"):
    """Write the excuses to dest_file

    Writes a list of excuses in a specified output_format to the
    path denoted by dest_file.  The output_format can either be "yaml"
    or "legacy-html".
    """
    excuselist = sorted(excuses.values(), key=lambda x: x.sortkey())
    if output_format == "yaml":
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
        opener = open
        if dest_file.endswith('.xz'):
            import lzma
            opener = lzma.open
        elif dest_file.endswith('.gz'):
            import gzip
            opener = gzip.open
        with opener(dest_file, 'wt', encoding='utf-8') as f:
            edatalist = [e.excusedata(excuses) for e in excuselist]
            excusesdata = {
                'sources': edatalist,
                'generated-date': datetime.utcnow(),
            }
            f.write(yaml.dump(excusesdata, default_flow_style=False, allow_unicode=True))
    elif output_format == "legacy-html":
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
        with open(dest_file, 'w', encoding='utf-8') as f:
            f.write("<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01//EN\" \"http://www.w3.org/TR/REC-html40/strict.dtd\">\n")
            f.write("<html><head><title>excuses...</title>")
            f.write("<meta http-equiv=\"Content-Type\" content=\"text/html;charset=utf-8\"></head><body>\n")
            f.write("<p>Generated: " + time.strftime("%Y.%m.%d %H:%M:%S %z", time.gmtime(time.time())) + "</p>\n")
            f.write("<ul>\n")
            for e in excuselist:
                f.write("<li>%s" % e.html(excuses))
            f.write("</ul></body></html>\n")
    else:   # pragma: no cover
        raise ValueError('Output format must be either "yaml or "legacy-html"')


def old_libraries(mi_factory, suite_info, outofsync_arches=frozenset()):
    """Detect old libraries left in the target suite for smooth transitions

    This method detects old libraries which are in the target suite but no
    longer built from the source package: they are still there because
    other packages still depend on them, but they should be removed as
    soon as possible.

    For "outofsync" architectures, outdated binaries are allowed to be in
    the target suite, so they are only added to the removal list if they
    are no longer in the (primary) source suite.
    """
    sources_t = suite_info.target_suite.sources
    binaries_t = suite_info.target_suite.binaries
    binaries_s = suite_info.primary_source_suite.binaries
    removals = []
    for arch in binaries_t:
        for pkg_name in binaries_t[arch]:
            pkg = binaries_t[arch][pkg_name]
            # FIXME: If linux-meta packages are old we break the suite consistency.
            if pkg.source.startswith("linux-meta") or pkg.source.startswith("linux-restricted-modules"):
                continue
            if sources_t[pkg.source].version != pkg.source_version and \
                    (arch not in outofsync_arches or pkg_name not in binaries_s[arch]):
                removals.append(mi_factory.generate_removal_for_cruft_item(pkg.pkg_id))
    return removals


def is_nuninst_asgood_generous(constraints, allow_uninst, architectures, old, new, break_arches=frozenset()):
    """Compares the nuninst counters and constraints to see if they improved

    Given a list of architectures, the previous and the current nuninst
    counters, this function determines if the current nuninst counter
    is better than the previous one.  Optionally it also accepts a set
    of "break_arches", the nuninst counter for any architecture listed
    in this set are completely ignored.

    If the nuninst counters are equal or better, then the constraints
    are checked for regressions (ignoring break_arches).

    Returns True if the new nuninst counter is better than the
    previous and there are no constraint regressions (ignoring Break-archs).
    Returns False otherwise.

    """
    diff = 0
    for arch in architectures:
        if arch in break_arches:
            continue
        diff = diff + \
            (len(new[arch] - allow_uninst[arch])
                - len(old[arch] - allow_uninst[arch]))
    if diff > 0:
        return False
    must_be_installable = constraints['keep-installable']
    for arch in architectures:
        if arch in break_arches:
            continue
        regression = new[arch] - old[arch]
        if not regression.isdisjoint(must_be_installable):
            return False
    return True


def clone_nuninst(nuninst, *, packages_s=None, architectures=None):
    """Completely or Selectively deep clone nuninst

    Given nuninst table, the package table for a given suite and
    a list of architectures, this function will clone the nuninst
    table.  Only the listed architectures will be deep cloned -
    the rest will only be shallow cloned.  When packages_s is given,
    packages not listed in packages_s will be pruned from the clone
    (if packages_s is omitted, the per architecture nuninst is cloned
    as-is)
    """
    clone = nuninst.copy()
    if architectures is None:
        return clone
    if packages_s is not None:
        for arch in architectures:
            clone[arch] = set(x for x in nuninst[arch] if x in packages_s[arch])
            clone[arch + "+all"] = set(x for x in nuninst[arch + "+all"] if x in packages_s[arch])
    else:
        for arch in architectures:
            clone[arch] = set(nuninst[arch])
            clone[arch + "+all"] = set(nuninst[arch + "+all"])
    return clone


def test_installability(target_suite, pkg_name, pkg_id, broken, nuninst_arch):
    """Test for installability of a package on an architecture

    (pkg_name, pkg_version, pkg_arch) is the package to check.

    broken is the set of broken packages.  If p changes
    installability (e.g. goes from uninstallable to installable),
    broken will be updated accordingly.

    If nuninst_arch is not None then it also updated in the same
    way as broken is.
    """
    c = 0
    r = target_suite.is_installable(pkg_id)
    if not r:
        # not installable
        if pkg_name not in broken:
            # regression
            broken.add(pkg_name)
            c = -1
        if nuninst_arch is not None and pkg_name not in nuninst_arch:
            nuninst_arch.add(pkg_name)
    else:
        if pkg_name in broken:
            # Improvement
            broken.remove(pkg_name)
            c = 1
        if nuninst_arch is not None and pkg_name in nuninst_arch:
            nuninst_arch.remove(pkg_name)
    return c


def check_installability(target_suite, binaries, arch, updates, check_archall, nuninst):
    broken = nuninst[arch + "+all"]
    packages_t_a = binaries[arch]

    for pkg_id in (x for x in updates if x.architecture == arch):
        name, version, parch = pkg_id
        if name not in packages_t_a:
            continue
        pkgdata = packages_t_a[name]
        if version != pkgdata.version:
            # Not the version in testing right now, ignore
            continue
        actual_arch = pkgdata.architecture
        nuninst_arch = None
        # only check arch:all packages if requested
        if check_archall or actual_arch != 'all':
            nuninst_arch = nuninst[parch]
        elif actual_arch == 'all':
            nuninst[parch].discard(name)
        test_installability(target_suite, name, pkg_id, broken, nuninst_arch)


def possibly_compressed(path, *, permitted_compressions=None):
    """Find and select a (possibly compressed) variant of a path

    If the given path exists, it will be returned

    :param path The base path.
    :param permitted_compressions An optional list of alternative extensions to look for.
      Defaults to "gz" and "xz".
    :returns The path given possibly with one of the permitted extensions.  Will raise a
     FileNotFoundError
    """
    if os.path.exists(path):
        return path
    if permitted_compressions is None:
        permitted_compressions = ['gz', 'xz']
    for ext in permitted_compressions:
        cpath = "%s.%s" % (path, ext)
        if os.path.exists(cpath):
            return cpath
    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)  # pragma: no cover


def create_provides_map(packages):
    """Create a provides map from a map binary package names and thier BinaryPackage objects

    :param packages: A dict mapping binary package names to their BinaryPackage object
    :return: A provides map
    """
    # create provides
    provides = defaultdict(set)

    for pkg, dpkg in packages.items():
        # register virtual packages and real packages that provide
        # them
        for provided_pkg, provided_version, _ in dpkg.provides:
            provides[provided_pkg].add((pkg, provided_version))

    return provides


def read_release_file(suite_dir):
    """Parses a given "Release" file

    :param suite_dir: The directory to the suite
    :return: A dict of the first (and only) paragraph in an Release file
    """
    release_file = os.path.join(suite_dir, 'Release')
    with open(release_file) as fd:
        tag_file = iter(apt_pkg.TagFile(fd))
        result = next(tag_file)
        if next(tag_file, None) is not None:  # pragma: no cover
            raise TypeError("%s has more than one paragraph" % release_file)
    return result


def read_sources_file(filename, sources=None, intern=sys.intern):
    """Parse a single Sources file into a hash

    Parse a single Sources file into a dict mapping a source package
    name to a SourcePackage object.  If there are multiple source
    packages with the same version, then highest versioned source
    package (that is not marked as "Extra-Source-Only") is the
    version kept in the dict.

    :param filename: Path to the Sources file.  Can be compressed by any algorithm supported by apt_pkg.TagFile
    :param sources: Optional dict to add the packages to.  If given, this is also the value returned.
    :param intern: Internal optimisation / implementation detail to avoid python's "LOAD_GLOBAL" instruction in a loop
    :return a dict mapping a name to a source package
    """
    if sources is None:
        sources = {}

    tag_file = apt_pkg.TagFile(filename)
    get_field = tag_file.section.get
    step = tag_file.step

    while step():
        if get_field('Extra-Source-Only', 'no') == 'yes':
            # Ignore sources only referenced by Built-Using
            continue
        pkg = get_field('Package')
        ver = get_field('Version')
        # There may be multiple versions of the source package
        # (in unstable) if some architectures have out-of-date
        # binaries.  We only ever consider the source with the
        # largest version for migration.
        if pkg in sources and apt_pkg.version_compare(sources[pkg].version, ver) > 0:
            continue
        maint = get_field('Maintainer')
        if maint:
            maint = intern(maint.strip())
        section = get_field('Section')
        if section:
            section = intern(section.strip())
        build_deps_arch = ", ".join(x for x in (get_field('Build-Depends'), get_field('Build-Depends-Arch'))
                                    if x is not None)
        if build_deps_arch != '':
            build_deps_arch = sys.intern(build_deps_arch)
        else:
            build_deps_arch = None
        build_deps_indep = get_field('Build-Depends-Indep')
        if build_deps_indep is not None:
            build_deps_indep = sys.intern(build_deps_indep)
        # XXX: Do the get_component thing in a much nicer way that can be upstreamed
        sources[intern(pkg)] = SourcePackage(intern(pkg),
                                             intern(ver),
                                             section,
                                             set(),
                                             maint,
                                             False,
                                             build_deps_arch,
                                             build_deps_indep,
                                             get_field('Testsuite', '').split(),
                                             get_field('Testsuite-Triggers', '').replace(',', '').split(),
                                             UbuntuComponent.get_component(section),
                                             )
    return sources


def get_dependency_solvers(block, binaries_s_a, provides_s_a, *, build_depends=False, empty_set=frozenset()):
    """Find the packages which satisfy a dependency block

    This method returns the list of packages which satisfy a dependency
    block (as returned by apt_pkg.parse_depends) in a package table
    for a given suite and architecture (a la self.binaries[suite][arch])

    It can also handle build-dependency relations if the named parameter
    "build_depends" is set to True.  In this case, block should be based
    on the return value from apt_pkg.parse_src_depends.

    :param block: The dependency block as parsed by apt_pkg.parse_depends (or apt_pkg.parse_src_depends
      if the "build_depends" is True)
    :param binaries_s_a: A dict mapping package names to the relevant BinaryPackage
    :param provides_s_a: A dict mapping package names to their providers (as generated by parse_provides)
    :param build_depends: If True, treat the "block" parameter as a build-dependency relation rather than
      a regular dependency relation.
    :param empty_set: Internal implementation detail / optimisation
    :return a list of package names solving the relation
    """
    packages = []

    # for every package, version and operation in the block
    for name, version, op in block:
        if ":" in name:
            name, archqual = name.split(":", 1)
        else:
            archqual = None

        # look for the package in unstable
        if name in binaries_s_a:
            package = binaries_s_a[name]
            # check the versioned dependency and architecture qualifier
            # (if present)
            if (op == '' and version == '') or apt_pkg.check_dep(package.version, op, version):
                if archqual is None:
                    packages.append(package)
                elif build_depends and archqual == 'native':
                    # Multi-arch handling for build-dependencies
                    # - :native is ok always (since dpkg 1.19.1)
                    packages.append(package)

                # Multi-arch handling for both build-dependencies and regular dependencies
                # - :any is ok iff the target has "M-A: allowed"
                if archqual == 'any' and package.multi_arch == 'allowed':
                    packages.append(package)

        # look for the package in the virtual packages list and loop on them
        for prov, prov_version in provides_s_a.get(name, empty_set):
            assert prov in binaries_s_a
            # A provides only satisfies:
            # - an unversioned dependency (per Policy Manual §7.5)
            # - a dependency without an architecture qualifier
            #   (per analysis of apt code)
            if archqual is not None:
                # Punt on this case - these days, APT and dpkg might actually agree on
                # this.
                continue
            if (op == '' and version == '') or \
                    (prov_version != '' and apt_pkg.check_dep(prov_version, op, version)):
                packages.append(binaries_s_a[prov])

    return packages


def invalidate_excuses(excuses, valid, invalid, invalidated):
    """Invalidate impossible excuses

    This method invalidates the impossible excuses, which depend
    on invalid excuses. The two parameters contains the sets of
    `valid' and `invalid' excuses.
    """

    # make a list of all packages (source and binary) that are present in the
    # excuses we have
    excuses_packages = defaultdict(set)
    for exc in excuses.values():
        for arch in exc.packages:
            for pkg_id in exc.packages[arch]:
                # note that the same package can be in multiple excuses
                # eg. when unstable and TPU have the same packages
                excuses_packages[pkg_id].add(exc.name)

    # create dependencies between excuses based on packages
    excuses_rdeps = defaultdict(set)
    for exc in excuses.values():
        # Note that excuses_rdeps is only populated by dependencies generated
        # based on packages below. There are currently no dependencies between
        # excuses that are added directly, so this is ok.

        for pkg_dep in exc.depends_packages:
            # set of excuses, each of which can satisfy this specific
            # dependency
            # if there is a dependency on a package that doesn't exist, the
            # set will contain an ImpossibleDependencyState
            dep_exc = set()
            for pkg_id in pkg_dep.deps:
                pkg_excuses = excuses_packages[pkg_id]
                # if the dependency isn't found, we get an empty set
                if pkg_excuses == frozenset():
                    imp_dep = ImpossibleDependencyState(
                            PolicyVerdict.REJECTED_PERMANENTLY,
                            "%s" % (pkg_id.name))
                    dep_exc.add(imp_dep)

                else:
                    dep_exc |= pkg_excuses
                    for e in pkg_excuses:
                        excuses_rdeps[e].add(exc.name)
            if not exc.add_dependency(dep_exc, pkg_dep.spec):
                valid.discard(exc.name)
                invalid.add(exc.name)

    # loop on the invalid excuses
    for ename in iter_except(invalid.pop, KeyError):
        invalidated.add(ename)
        # if there is no reverse dependency, skip the item
        if ename not in excuses_rdeps:
            continue

        rdep_verdict = PolicyVerdict.REJECTED_WAITING_FOR_ANOTHER_ITEM
        if excuses[ename].policy_verdict.is_blocked:
            rdep_verdict = PolicyVerdict.REJECTED_BLOCKED_BY_ANOTHER_ITEM

        # loop on the reverse dependencies
        for x in excuses_rdeps[ename]:
            exc = excuses[x]
            # if the item is valid and it is not marked as `forced', then we
            # invalidate this specfic dependency

            if x in valid and not exc.forced:
                # mark this specific dependency as invalid
                still_valid = exc.invalidate_dependency(ename, rdep_verdict)

                # if there are no alternatives left for this dependency,
                # invalidate the excuse
                if not still_valid:
                    valid.discard(x)
                    invalid.add(x)


def compile_nuninst(target_suite, architectures, nobreakall_arches):
    """Compile a nuninst dict from the current testing

    :param target_suite: The target suite
    :param architectures: List of architectures
    :param nobreakall_arches: List of architectures where arch:all packages must be installable
    """
    nuninst = {}
    binaries_t = target_suite.binaries

    # for all the architectures
    for arch in architectures:
        # if it is in the nobreakall ones, check arch-independent packages too
        check_archall = arch in nobreakall_arches

        # check all the packages for this architecture
        nuninst[arch] = set()
        packages_t_a = binaries_t[arch]
        for pkg_name, pkg_data in packages_t_a.items():
            r = target_suite.is_installable(pkg_data.pkg_id)
            if not r:
                nuninst[arch].add(pkg_name)

        # if they are not required, remove architecture-independent packages
        nuninst[arch + "+all"] = nuninst[arch].copy()
        if not check_archall:
            for pkg_name in nuninst[arch + "+all"]:
                pkg_data = packages_t_a[pkg_name]
                if pkg_data.architecture == 'all':
                    nuninst[arch].remove(pkg_name)

    return nuninst


def is_smooth_update_allowed(binary, smooth_updates, hints):
    if 'ALL' in smooth_updates:
        return True
    section = binary.section.split('/')[-1]
    if section in smooth_updates:
        return True
    if hints.search('allow-smooth-update', package=binary.source, version=binary.source_version):
        # note that this needs to match the source version *IN TESTING*
        return True
    return False


def find_smooth_updateable_binaries(binaries_to_check,
                                    source_data,
                                    pkg_universe,
                                    target_suite,
                                    binaries_t,
                                    binaries_s,
                                    removals,
                                    smooth_updates,
                                    hints):
    check = set()
    smoothbins = set()

    for pkg_id in binaries_to_check:
        binary, _, parch = pkg_id

        cruftbins = set()

        # Not a candidate for smooth up date (newer non-cruft version in unstable)
        if binary in binaries_s[parch]:
            if binaries_s[parch][binary].source_version == source_data.version:
                continue
            cruftbins.add(binaries_s[parch][binary].pkg_id)

        # Maybe a candidate (cruft or removed binary): check if config allows us to smooth update it.
        if is_smooth_update_allowed(binaries_t[parch][binary], smooth_updates, hints):
            # if the package has reverse-dependencies which are
            # built from other sources, it's a valid candidate for
            # a smooth update.  if not, it may still be a valid
            # candidate if one if its r-deps is itself a candidate,
            # so note it for checking later
            rdeps = set(pkg_universe.reverse_dependencies_of(pkg_id))
            # We ignore all binaries listed in "removals" as we
            # assume they will leave at the same time as the
            # given package.
            rdeps.difference_update(removals, binaries_to_check)

            smooth_update_it = False
            if target_suite.any_of_these_are_in_the_suite(rdeps):
                combined = set(smoothbins)
                combined.add(pkg_id)
                for rdep in rdeps:
                    # each dependency clause has a set of possible
                    # alternatives that can satisfy that dependency.
                    # if any of them is outside the set of smoothbins, the
                    # dependency can be satisfied even if this binary was
                    # removed, so there is no need to keep it around for a
                    # smooth update
                    # if not, only this binary can satisfy the dependency, so
                    # we should keep it around until the rdep is no longer in
                    # testing
                    for dep_clause in pkg_universe.dependencies_of(rdep):
                        # filter out cruft binaries from unstable, because
                        # they will not be added to the set of packages that
                        # will be migrated
                        if dep_clause - cruftbins <= combined:
                            smooth_update_it = True
                            break

            if smooth_update_it:
                smoothbins = combined
            else:
                check.add(pkg_id)

    # check whether we should perform a smooth update for
    # packages which are candidates but do not have r-deps
    # outside of the current source
    while 1:
        found_any = False
        for pkg_id in check:
            rdeps = pkg_universe.reverse_dependencies_of(pkg_id)
            if not rdeps.isdisjoint(smoothbins):
                smoothbins.add(pkg_id)
                found_any = True
        if not found_any:
            break
        check = [x for x in check if x not in smoothbins]

    return smoothbins


def find_newer_binaries(suite_info, pkg, add_source_for_dropped_bin=False):
    """
    Find newer binaries for pkg in any of the source suites.

    :param pkg BinaryPackage (is assumed to be in the target suite)

    :param add_source_for_dropped_bin If True, newer versions of the
      source of pkg will be added if they don't have the binary pkg
    """

    source = pkg.source
    newer_versions = []
    for suite in suite_info:
        if suite.suite_class == SuiteClass.TARGET_SUITE:
            continue

        suite_binaries_on_arch = suite.binaries.get(pkg.pkg_id.architecture)
        if not suite_binaries_on_arch:
            continue

        newerbin = None
        if pkg.pkg_id.package_name in suite_binaries_on_arch:
            newerbin = suite_binaries_on_arch[pkg.pkg_id.package_name]
            if suite.is_cruft(newerbin):
                # We pretend the cruft binary doesn't exist.
                # We handle this as if the source didn't have the binary
                # (see below)
                newerbin = None
            elif apt_pkg.version_compare(newerbin.version, pkg.version) <= 0:
                continue
        else:
            if source not in suite.sources:
                # bin and source not in suite: no newer version
                continue

        if not newerbin:
            if not add_source_for_dropped_bin:
                continue
            # We only get here if there is a newer version of the source,
            # which doesn't have the binary anymore (either it doesn't
            # exist, or it's cruft and we pretend it doesn't exist).
            # Add the new source instead.
            nsrc = suite.sources[source]
            n_id = PackageId(source, nsrc.version, "source")
            overs = pkg.source_version
            if apt_pkg.version_compare(nsrc.version, overs) <= 0:
                continue
        else:
            n_id = newerbin.pkg_id

        newer_versions.append((n_id, suite))

    return newer_versions


def parse_provides(provides_raw, pkg_id=None, logger=None):
    parts = apt_pkg.parse_depends(provides_raw, False)
    nprov = []
    for or_clause in parts:
        if len(or_clause) != 1:  # pragma: no cover
            if logger is not None:
                msg = "Ignoring invalid provides in %s: Alternatives [%s]"
                logger.warning(msg, str(pkg_id), str(or_clause))
            continue
        for part in or_clause:
            provided, provided_version, op = part
            if op != '' and op != '=':  # pragma: no cover
                if logger is not None:
                    msg = "Ignoring invalid provides in %s: %s (%s %s)"
                    logger.warning(msg, str(pkg_id), provided, op, provided_version)
                continue
            provided = sys.intern(provided)
            provided_version = sys.intern(provided_version)
            part = (provided, provided_version, sys.intern(op))
            nprov.append(part)
    return nprov


def parse_builtusing(builtusing_raw, pkg_id=None, logger=None):
    parts = apt_pkg.parse_depends(builtusing_raw, False)
    nbu = []
    for or_clause in parts:
        if len(or_clause) != 1:  # pragma: no cover
            if logger is not None:
                msg = "Ignoring invalid builtusing in %s: Alternatives [%s]"
                logger.warning(msg, str(pkg_id), str(or_clause))
            continue
        for part in or_clause:
            bu, bu_version, op = part
            if op != '=':  # pragma: no cover
                if logger is not None:
                    msg = "Ignoring invalid builtusing in %s: %s (%s %s)"
                    logger.warning(msg, str(pkg_id), bu, op, bu_version)
                continue
            bu = sys.intern(bu)
            bu_version = sys.intern(bu_version)
            part = (bu, bu_version)
            nbu.append(part)
    return nbu


@unique
class UbuntuComponent(Enum):
    MAIN = 'main'
    RESTRICTED = 'restricted'
    UNIVERSE = 'universe'
    MULTIVERSE = 'multiverse'

    @classmethod
    def get_component(cls, section):
        """Parse section and return component

        Given a section, return component. Packages in MAIN have no
        prefix, all others have <component>/ prefix.
        """
        name2component = {
            "restricted": cls.RESTRICTED,
            "universe": cls.UNIVERSE,
            "multiverse": cls.MULTIVERSE,
        }

        if "/" in section:
            return name2component[section.split("/", 1)[0]]

        return cls.MAIN

    def allowed_component(self, dep):
        """Check if I can depend on the other component"""

        component_dependencies = {
            UbuntuComponent.MAIN: [UbuntuComponent.MAIN],
            UbuntuComponent.RESTRICTED: [
                UbuntuComponent.MAIN,
                UbuntuComponent.RESTRICTED,
            ],
            UbuntuComponent.UNIVERSE: [
                UbuntuComponent.MAIN,
                UbuntuComponent.UNIVERSE,
            ],
            UbuntuComponent.MULTIVERSE: [
                UbuntuComponent.MAIN,
                UbuntuComponent.RESTRICTED,
                UbuntuComponent.UNIVERSE,
                UbuntuComponent.MULTIVERSE,
            ],
        }

        return dep in component_dependencies[self]
