import os
import json
import socket
import urllib.request
import urllib.parse

from collections import defaultdict
from urllib.error import HTTPError

from britney2 import SuiteClass
from britney2.policies.rest import Rest
from britney2.policies.policy import BasePolicy, PolicyVerdict


LAUNCHPAD_URL = "https://api.launchpad.net/1.0/"
PRIMARY = LAUNCHPAD_URL + "ubuntu/+archive/primary"
INCLUDE = ["~bileto-ppa-service/", "~ci-train-ppa-service/"]
EXCLUDE = ["~ci-train-ppa-service/+archive/ubuntu/4810", "~ci-train-ppa-service/+archive/ubuntu/4813", "~ci-train-ppa-service/+archive/ubuntu/4815", "~ci-train-ppa-service/+archive/ubuntu/4816"]


class SourcePPAPolicy(BasePolicy, Rest):
    """Migrate packages copied from same source PPA together

    This policy will query launchpad to determine what source PPA packages
    were copied from, and ensure that all packages from the same PPA migrate
    together.
    """

    def __init__(self, options, suite_info):
        super().__init__(
            "source-ppa", options, suite_info, {SuiteClass.PRIMARY_SOURCE_SUITE}
        )
        self.filename = os.path.join(options.unstable, "SourcePPA")
        # Dict of dicts; maps pkg name -> pkg version -> source PPA URL
        self.source_ppas_by_pkg = defaultdict(dict)
        # Dict of sets; maps source PPA URL -> (set of source names, set of
        # friends; collected excuses for this ppa)
        self.excuses_by_source_ppa = defaultdict(set)
        self.source_ppa_info_by_source_ppa = defaultdict(set)
        self.britney = None
        # self.cache contains self.source_ppas_by_pkg from previous run
        self.cache = {}

    def lp_get_source_ppa(self, pkg, version):
        """Ask LP what source PPA pkg was copied from"""
        cached = self.cache.get(pkg, {}).get(version)
        if cached is not None:
            return cached

        data = self.query_lp_rest_api(
            "%s/+archive/primary" % self.options.distribution,
            {
                "ws.op": "getPublishedSources",
                "pocket": "Proposed",
                "source_name": pkg,
                "version": version,
                "exact_match": "true",
                "distro_series": "/%s/%s"
                % (self.options.distribution, self.options.series),
            },
        )
        try:
            sourcepub = data["entries"][0]["self_link"]
        # IndexError means no packages in -proposed matched this name/version,
        # which is expected to happen when bileto runs britney.
        except IndexError:
            self.logger.info(
                "SourcePPA getPackageUploads IndexError (%s %s)"
                % (pkg, version)
            )
            return "IndexError"
        data = self.query_lp_rest_api(
            sourcepub, {"ws.op": "getPublishedBinaries"}
        )
        for binary in data["entries"]:
            link = binary["build_link"] or ""
            if "/+archive/" in link:
                ppa, _, buildid = link.partition("/+build/")
                return ppa
        return ""

    def initialise(self, britney):
        """Load cached source ppa data"""
        super().initialise(britney)
        self.britney = britney

        if os.path.exists(self.filename):
            with open(self.filename, encoding="utf-8") as data:
                self.cache = json.load(data)
            self.logger.info(
                "Loaded cached source ppa data from %s", self.filename
            )

    def apply_src_policy_impl(
        self,
        sourceppa_info,
        item,
        source_data_tdist,
        source_data_srcdist,
        excuse,
    ):
        """Reject package if any other package copied from same PPA is invalid"""
        source_name = item.package
        accept = excuse.is_valid
        version = source_data_srcdist.version
        sourceppa = self.lp_get_source_ppa(source_name, version) or ""
        verdict = excuse.policy_verdict
        self.source_ppas_by_pkg[source_name][version] = sourceppa
        if [team for team in EXCLUDE if team in sourceppa]:
            return PolicyVerdict.PASS
        if not [team for team in INCLUDE if team in sourceppa]:
            return PolicyVerdict.PASS

        # check for a force hint; we have to check here in addition to
        # checking in britney.py, otherwise /this/ package will later be
        # considered valid candidate but all the /others/ from the ppa will
        # be invalidated via this policy and not fixed by the force hint.
        forces = self.hints.search(
            "force", package=source_name, version=source_data_srcdist.version
        )
        if forces:
            excuse.dontinvalidate = True
            changed_state = excuse.force()
            if changed_state:
                excuse.addhtml(
                    "Should ignore, but forced by %s" % (forces[0].user)
                )
            accept = True

        shortppa = sourceppa.replace(LAUNCHPAD_URL, "")
        sourceppa_info[source_name] = shortppa

        if not excuse.is_valid:
            self.logger.info(
                "sourceppa: processing %s, which is invalid, will invalidate set",
                source_name,
            )
        else:
            # Check for other packages that might invalidate this one
            for friend_exc in self.excuses_by_source_ppa[sourceppa]:
                sourceppa_info[friend_exc.item.package] = shortppa
                if not friend_exc.is_valid:
                    self.logger.info(
                        "sourceppa: processing %s, found invalid grouped package %s, will invalidate set"
                        % (source_name, friend_exc.name)
                    )
                    accept = False
                    break

        self.excuses_by_source_ppa[sourceppa].add(excuse)

        if not accept:
            # Invalidate all packages in this source ppa
            for friend_exc in self.excuses_by_source_ppa[sourceppa]:
                self.logger.info("friend: %s", friend_exc.name)
                sourceppa_info[friend_exc.item.package] = shortppa
                if friend_exc.is_valid:
                    if friend_exc == excuse:
                        verdict = PolicyVerdict.REJECTED_WAITING_FOR_ANOTHER_ITEM
                    else:
                        friend_exc.invalidate_externally(
                            PolicyVerdict.REJECTED_WAITING_FOR_ANOTHER_ITEM
                        )
                    friend_exc.addreason("source-ppa")
                    self.logger.info(
                        "sourceppa: ... invalidating %s due to the above (ppa: %s), %s"
                        % (friend_exc.name, shortppa, sourceppa_info)
                    )
                    friend_exc.addinfo("Grouped with PPA %s" % shortppa)

            for friend_exc in self.excuses_by_source_ppa[sourceppa]:
                try:
                    friend_exc.policy_info["source-ppa"].update(sourceppa_info)
                except KeyError:
                    friend_exc.policy_info["source-ppa"] = sourceppa_info.copy()

        return verdict

    def save_state(self, britney):
        """Write source ppa data to disk"""
        tmp = self.filename + ".tmp"
        with open(tmp, "w", encoding="utf-8") as data:
            json.dump(self.source_ppas_by_pkg, data)
        os.rename(tmp, self.filename)
        self.logger.info("Wrote source ppa data to %s" % self.filename)
