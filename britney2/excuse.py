# -*- coding: utf-8 -*-

# Copyright (C) 2001-2004 Anthony Towns <ajt@debian.org>
#                         Andreas Barth <aba@debian.org>
#                         Fabio Tranchitella <kobold@debian.org>

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

from collections import defaultdict
import re

from britney2 import DependencyType
from britney2.excusedeps import DependencySpec, DependencyState, ImpossibleDependencyState
from britney2.policies.policy import PolicyVerdict

VERDICT2DESC = {
    PolicyVerdict.PASS:
        'Will attempt migration (Any information below is purely informational)',
    PolicyVerdict.PASS_HINTED:
        'Will attempt migration due to a hint (Any information below is purely informational)',
    PolicyVerdict.REJECTED_TEMPORARILY:
        'Waiting for test results, another package or too young (no action required now - check later)',
    PolicyVerdict.REJECTED_WAITING_FOR_ANOTHER_ITEM:
        'Waiting for another item to be ready to migrate (no action required now - check later)',
    PolicyVerdict.REJECTED_BLOCKED_BY_ANOTHER_ITEM:
        'BLOCKED: Cannot migrate due to another item, which is blocked (please check which dependencies are stuck)',
    PolicyVerdict.REJECTED_NEEDS_APPROVAL:
        'BLOCKED: Needs an approval (either due to a freeze, the source suite or a manual hint)',
    PolicyVerdict.REJECTED_CANNOT_DETERMINE_IF_PERMANENT:
        'BLOCKED: Maybe temporary, maybe blocked but Britney is missing information (check below)',
    PolicyVerdict.REJECTED_PERMANENTLY:
        'BLOCKED: Rejected/violates migration policy/introduces a regression',
}


class ExcuseDependency(object):
    """Object to represent a specific dependecy of an excuse on a package
       (source or binary) or on other excuses"""

    def __init__(self, spec, depstates):
        """
        :param: spec: DependencySpec
        :param: depstates: list of DependencyState, each of which can satisfy
                           the dependency
        """
        self.spec = spec
        self.depstates = depstates

    @property
    def deptype(self):
        return self.spec.deptype

    @property
    def valid(self):
        if {d for d in self.depstates if d.valid}:
            return True
        else:
            return False

    @property
    def deps(self):
        return {d.dep for d in self.depstates}

    @property
    def possible(self):
        if {d for d in self.depstates if d.possible}:
            return True
        else:
            return False

    @property
    def first_dep(self):
        """return the first valid dependency, if there is one, otherwise the
           first possible one

           return None if there are only impossible dependencies
        """
        first = None
        for d in self.depstates:
            if d.valid:
                return d.dep
            elif d.possible and not first:
                first = d.dep
        return first

    @property
    def first_impossible_dep(self):
        """return the first impossible dependency, if there is one"""
        first = None
        for d in self.depstates:
            if not d.possible:
                return d.desc
        return first

    @property
    def verdict(self):
        return min({d.verdict for d in self.depstates})

    def invalidate(self, excuse, verdict):
        """invalidate the dependencies on a specific excuse

        :param excuse: the excuse which is no longer valid
        :param verdict: the PolicyVerdict causing the invalidation
        """
        invalidated_alternative = False
        valid_alternative_left = False
        for ds in self.depstates:
            if ds.dep == excuse:
                ds.invalidate(verdict)
                invalidated_alternative = True
            elif ds.valid:
                valid_alternative_left = True

        return valid_alternative_left


class Excuse(object):
    """Excuse class

    This class represents an update excuse, which is a detailed explanation
    of why a package can or cannot be updated in the testing distribution from
    a newer package in another distribution (like for example unstable).

    The main purpose of the excuses is to be written in an HTML file which
    will be published over HTTP. The maintainers will be able to parse it
    manually or automatically to find the explanation of why their packages
    have been updated or not.
    """

    # @var reemail
    # Regular expression for removing the email address
    reemail = re.compile(r" *<.*?>")

    def __init__(self, migrationitem):
        """Class constructor

        This method initializes the excuse with the specified name and
        the default values.
        """
        self.item = migrationitem
        self.ver = ("-", "-")
        self.maint = None
        self.daysold = None
        self.mindays = None
        self.section = None
        self._is_valid = False
        self.needs_approval = False
        self.hints = []
        self.forced = False
        self._policy_verdict = PolicyVerdict.REJECTED_PERMANENTLY

        self.all_deps = []
        self.break_deps = []
        self.unsatisfiable_on_archs = []
        self.unsat_deps = defaultdict(set)
        self.newbugs = set()
        self.oldbugs = set()
        self.reason = {}
        self.htmlline = []
        self.missing_builds = set()
        self.missing_builds_ood_arch = set()
        self.old_binaries = defaultdict(set)
        self.policy_info = {}
        self.verdict_info = defaultdict(list)
        self.infoline = []
        self.detailed_info = []
        self.dep_info_rendered = False

        # packages (source and binary) that will migrate to testing if the
        # item from this excuse migrates
        self.packages = defaultdict(set)

        # list of ExcuseDependency, with dependencies on packages
        self.depends_packages = []
        # contains all PackageIds in any over the sets above
        self.depends_packages_flattened = set()

        self.bounty = {}
        self.penalty = {}

    def sortkey(self):
        if self.daysold is None:
            return (-1, self.uvname)
        return (self.daysold, self.uvname)

    @property
    def name(self):
        return self.item.name

    @property
    def uvname(self):
        return self.item.uvname

    @property
    def source(self):
        return self.item.package

    @property
    def is_valid(self):
        return False if self._policy_verdict.is_rejected else True

    @property
    def policy_verdict(self):
        return self._policy_verdict

    @policy_verdict.setter
    def policy_verdict(self, value):
        if value.is_rejected and self.forced:
            # By virtue of being forced, the item was hinted to
            # undo the rejection
            value = PolicyVerdict.PASS_HINTED
        self._policy_verdict = value

    def set_vers(self, tver, uver):
        """Set the versions of the item from target and source suite"""
        if tver and uver:
            self.ver = (tver, uver)
        elif tver:
            self.ver = (tver, self.ver[1])
        elif uver:
            self.ver = (self.ver[0], uver)

    def set_maint(self, maint):
        """Set the package maintainer's name"""
        self.maint = self.reemail.sub("", maint)

    def set_section(self, section):
        """Set the section of the package"""
        self.section = section

    def add_dependency(self, dep, spec):
        """Add a dependency of type deptype

        :param dep: set with names of excuses, each of which satisfies the dep
        :param spec: DependencySpec

        """

        assert dep != frozenset(), "%s: Adding empty list of dependencies" % self.name

        deps = []
        for d in dep:
            if isinstance(d, DependencyState):
                deps.append(d)
            else:
                deps.append(DependencyState(d))
        ed = ExcuseDependency(spec, deps)
        self.all_deps.append(ed)
        if not ed.valid:
            self.do_invalidate(ed)
        return ed.valid

    def get_deps(self):
        # the autohinter uses the excuses data to query dependencies between
        # excuses. For now, we keep the current behaviour by just returning
        # the data that was in the old deps set
        """ Get the dependencies of type DEPENDS """
        deps = set()
        for dep in [d for d in self.all_deps if d.deptype == DependencyType.DEPENDS]:
            # add the first valid dependency
            for d in dep.depstates:
                if d.valid:
                    deps.add(d.dep)
                    break
        return deps

    def add_break_dep(self, name, arch):
        """Add a break dependency"""
        if (name, arch) not in self.break_deps:
            self.break_deps.append((name, arch))

    def add_unsatisfiable_on_arch(self,  arch):
        """Add an arch that has unsatisfiable dependencies"""
        if arch not in self.unsatisfiable_on_archs:
            self.unsatisfiable_on_archs.append(arch)

    def add_unsatisfiable_dep(self, signature, arch):
        """Add an unsatisfiable dependency"""
        self.unsat_deps[arch].add(signature)

    def do_invalidate(self, dep):
        """
        param: dep: ExcuseDependency
        """
        self.addreason(dep.deptype.get_reason())
        if self.policy_verdict < dep.verdict:
            self.policy_verdict = dep.verdict

    def invalidate_dependency(self, name, verdict):
        """Invalidate dependency"""
        invalidate = False

        for dep in self.all_deps:
            if not dep.invalidate(name, verdict):
                invalidate = True
                self.do_invalidate(dep)

        return not invalidate

    def setdaysold(self, daysold, mindays):
        """Set the number of days from the upload and the minimum number of days for the update"""
        self.daysold = daysold
        self.mindays = mindays

    def force(self):
        """Add force hint"""
        self.forced = True
        if self._policy_verdict.is_rejected:
            self._policy_verdict = PolicyVerdict.PASS_HINTED
            return True
        return False

    def addinfo(self, note):
        """Add a note in HTML"""
        self.infoline.append(note)

    def add_verdict_info(self, verdict, note):
        """Add a note to info about this verdict level"""
        self.verdict_info[verdict].append(note)

    def add_detailed_info(self, note):
        """Add a note to detailed info"""
        self.detailed_info.append(note)

    def missing_build_on_arch(self, arch):
        """Note that the item is missing a build on a given architecture"""
        self.missing_builds.add(arch)

    def missing_build_on_ood_arch(self, arch):
        """Note that the item is missing a build on a given "out of date" architecture"""
        self.missing_builds.add(arch)

    def add_old_binary(self, binary, from_source_version):
        """Denote than an old binary ("cruft") is available from a previous source version"""
        self.old_binaries[from_source_version].add(binary)

    def add_hint(self, hint):
        self.hints.append(hint)

    def add_package(self, pkg_id):
        self.packages[pkg_id.architecture].add(pkg_id)

    def add_package_depends(self, spec, depends):
        """Add dependency on a package (source or binary)

        :param spec: DependencySpec
        :param depends: set of PackageIds (source or binary), each of which can satisfy the dependency
        """

        assert depends != frozenset(), "%s: Adding empty list of package dependencies" % self.name

        # we use DependencyState for consistency with excuse dependencies, but
        # package dependencies are never invalidated, they are used to add
        # excuse dependencies (in invalidate_excuses()), and these are
        # (potentially) invalidated
        ed = ExcuseDependency(spec, [DependencyState(d) for d in depends])
        self.depends_packages.append(ed)
        self.depends_packages_flattened |= depends

    def _format_verdict_summary(self):
        verdict = self._policy_verdict
        if verdict in VERDICT2DESC:
            return VERDICT2DESC[verdict]
        return "UNKNOWN: Missing description for {0} - Please file a bug against Britney".format(verdict.name)

    def _render_dep_issues(self, excuses):
        if self.dep_info_rendered:
            return

        dep_issues = defaultdict(set)
        for d in self.all_deps:
            dep = d.first_dep
            info = ""
            if not d.possible:
                desc = d.first_impossible_dep
                info = "Impossible %s: %s -> %s" % (d.deptype, self.uvname, desc)
            else:
                duv = excuses[dep].uvname
                if d.valid:
                    info = "%s: %s <a href=\"#%s\">%s</a>" % (d.deptype, self.uvname, duv, duv)
                else:
                    info = "%s: %s <a href=\"#%s\">%s</a> (not considered)" % (d.deptype, self.uvname, duv, duv)
                    dep_issues[d.verdict].add("Invalidated by %s" % d.deptype.get_description())
            dep_issues[d.verdict].add(info)

        seen = set()
        for v in sorted(dep_issues.keys(), reverse=True):
            for i in sorted(dep_issues[v]):
                if i not in seen:
                    self.add_verdict_info(v, i)
                    seen.add(i)

        self.dep_info_rendered = True

    def html(self, excuses):
        """Render the excuse in HTML"""
        res = "<a id=\"%s\" name=\"%s\">%s</a> (%s to %s)\n<ul>\n" % \
            (self.uvname, self.uvname, self.uvname, self.ver[0], self.ver[1])
        info = self._text(excuses)
        for l in info:
            res += "<li>%s\n" % l
        res = res + "</ul>\n"
        return res

    def setbugs(self, oldbugs, newbugs):
        """"Set the list of old and new bugs"""
        self.newbugs.update(newbugs)
        self.oldbugs.update(oldbugs)

    def addreason(self, reason):
        """"adding reason"""
        self.reason[reason] = 1

    def hasreason(self, reason):
        return reason in self.reason

    def _text(self, excuses):
        """Render the excuse in text"""
        self._render_dep_issues(excuses)
        res = []
        res.append(
            "Migration status for %s (%s to %s): %s" %
            (self.uvname, self.ver[0], self.ver[1], self._format_verdict_summary()))
        if not self.is_valid:
            res.append("Issues preventing migration:")
        for v in sorted(self.verdict_info.keys(), reverse=True):
            for x in self.verdict_info[v]:
                res.append("" + x + "")
        if self.infoline:
            res.append("Additional info:")
            for x in self.infoline:
                res.append("" + x + "")
        if self.htmlline:
            res.append("Legacy info:")
            for x in self.htmlline:
                res.append("" + x + "")
        return res

    def excusedata(self, excuses):
        """Render the excuse in as key-value data"""
        excusedata = {}
        excusedata["excuses"] = self._text(excuses)
        excusedata["item-name"] = self.uvname
        excusedata["source"] = self.source
        excusedata["migration-policy-verdict"] = self._policy_verdict.name
        excusedata["old-version"] = self.ver[0]
        excusedata["new-version"] = self.ver[1]
        if self.maint:
            excusedata['maintainer'] = self.maint
        if self.section and self.section.find("/") > -1:
            excusedata['component'] = self.section.split('/')[0]
        if self.policy_info:
            excusedata['policy_info'] = self.policy_info
        if self.missing_builds or self.missing_builds_ood_arch:
            excusedata['missing-builds'] = {
                'on-architectures': sorted(self.missing_builds),
                'on-unimportant-architectures': sorted(self.missing_builds_ood_arch),
            }
        if {d for d in self.all_deps if not d.valid and d.possible}:
            excusedata['invalidated-by-other-package'] = True
        if self.all_deps \
                or self.break_deps or self.unsat_deps:
            excusedata['dependencies'] = dep_data = {}

            migrate_after = set(d.first_dep for d in self.all_deps if d.valid)
            blocked_by = set(d.first_dep for d in self.all_deps
                             if not d.valid and d.possible)

            break_deps = [x for x, _ in self.break_deps if
                          x not in migrate-after and
                          x not in blocked-by]

            def sorted_uvnames(deps):
                return sorted(excuses[d].uvname for d in deps)

            if blocked_by:
                dep_data['blocked-by'] = sorted_uvnames(blocked_by)
            if migrate_after:
                dep_data['migrate-after'] = sorted_uvnames(migrate_after)
            if break_deps:
                dep_data['unimportant-dependencies'] = sorted_uvnames(break_deps)
            if self.unsat_deps:
                dep_data['unsatisfiable-dependencies'] = {x: sorted(self.unsat_deps[x]) for x in self.unsat_deps}
        if self.needs_approval:
            status = 'not-approved'
            if any(h.type == 'unblock' for h in self.hints):
                status = 'approved'
            excusedata['manual-approval-status'] = status
        if self.hints:
            hint_info = [{
                             'hint-type': h.type,
                             'hint-from': h.user,
                         } for h in self.hints]

            excusedata['hints'] = hint_info
        if self.old_binaries:
            excusedata['old-binaries'] = {x: sorted(self.old_binaries[x]) for x in self.old_binaries}
        if self.forced:
            excusedata["forced-reason"] = sorted(list(self.reason.keys()))
            excusedata["reason"] = []
        else:
            excusedata["reason"] = sorted(list(self.reason.keys()))
        excusedata["is-candidate"] = self.is_valid
        if self.detailed_info:
            di = []
            for x in self.detailed_info:
                di.append("" + x + "")
            excusedata["detailed-info"] = di
        return excusedata

    def add_bounty(self, policy, bounty):
        """"adding bounty"""
        self.bounty[policy] = bounty

    def add_penalty(self, policy, penalty):
        """"adding penalty"""
        self.penalty[policy] = penalty
