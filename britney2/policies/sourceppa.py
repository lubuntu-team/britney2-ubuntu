import os
import json
import socket
import urllib.request
import urllib.parse

from collections import defaultdict
from urllib.error import HTTPError

from britney2.policies.rest import Rest
from britney2.policies.policy import BasePolicy, PolicyVerdict


LAUNCHPAD_URL = 'https://api.launchpad.net/1.0/'
PRIMARY = LAUNCHPAD_URL + 'ubuntu/+archive/primary'
INCLUDE = [
    '~bileto-ppa-service/',
    '~ci-train-ppa-service/',
]


class SourcePPAPolicy(BasePolicy, Rest):
    """Migrate packages copied from same source PPA together

    This policy will query launchpad to determine what source PPA packages
    were copied from, and ensure that all packages from the same PPA migrate
    together.
    """

    def __init__(self, options, suite_info):
        super().__init__('source-ppa', options, suite_info, {'unstable'})
        self.filename = os.path.join(options.unstable, 'SourcePPA')
        # Dict of dicts; maps pkg name -> pkg version -> source PPA URL
        self.source_ppas_by_pkg = defaultdict(dict)
        # Dict of sets; maps source PPA URL -> set of source names
        self.pkgs_by_source_ppa = defaultdict(set)
        self.britney = None
        # self.cache contains self.source_ppas_by_pkg from previous run
        self.cache = {}

    def lp_get_source_ppa(self, pkg, version):
        """Ask LP what source PPA pkg was copied from"""
        cached = self.cache.get(pkg, {}).get(version)
        if cached is not None:
            return cached

        data = self.query_lp_rest_api('%s/+archive/primary' % self.options.distribution, {
            'ws.op': 'getPublishedSources',
            'pocket': 'Proposed',
            'source_name': pkg,
            'version': version,
            'exact_match': 'true',
            'distro_series': '/%s/%s' % (self.options.distribution, self.options.series),
        })
        try:
            sourcepub = data['entries'][0]['self_link']
        # IndexError means no packages in -proposed matched this name/version,
        # which is expected to happen when bileto runs britney.
        except IndexError:
            self.log('SourcePPA getPackageUploads IndexError (%s %s)' % (pkg, version))
            return 'IndexError'
        data = self.query_lp_rest_api(sourcepub, {'ws.op': 'getPublishedBinaries'})
        for binary in data['entries']:
            link = binary['build_link'] or ''
            if '/+archive/' in link:
                ppa, _, buildid = link.partition('/+build/')
                return ppa
        return ''

    def initialise(self, britney):
        """Load cached source ppa data"""
        super().initialise(britney)
        self.britney = britney

        if os.path.exists(self.filename):
            with open(self.filename, encoding='utf-8') as data:
                self.cache = json.load(data)
            self.log("Loaded cached source ppa data from %s" % self.filename)

    def apply_policy_impl(self, sourceppa_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        """Reject package if any other package copied from same PPA is invalid"""
        accept = excuse.is_valid
        britney_excuses = self.britney.excuses
        version = source_data_srcdist.version
        sourceppa = self.lp_get_source_ppa(source_name, version) or ''
        self.source_ppas_by_pkg[source_name][version] = sourceppa
        if not [team for team in INCLUDE if team in sourceppa]:
            return PolicyVerdict.PASS

        shortppa = sourceppa.replace(LAUNCHPAD_URL, '')
        sourceppa_info[source_name] = shortppa
        # Check for other packages that might invalidate this one
        for friend in self.pkgs_by_source_ppa[sourceppa]:
            sourceppa_info[friend] = shortppa
            if not britney_excuses[friend].is_valid:
                self.log ("sourceppa: processing %s, found invalid grouped package %s, will invalidate set"  % (source_name, britney_excuses[friend].name))
                accept = False
        self.pkgs_by_source_ppa[sourceppa].add(source_name)

        if not accept:
            # Invalidate all packages in this source ppa
            for friend in self.pkgs_by_source_ppa[sourceppa]:
                friend_exc = britney_excuses.get(friend, excuse)
                if friend_exc.is_valid:
                    friend_exc.is_valid = False
                    friend_exc.addreason('source-ppa')
                    friend_exc.policy_info['source-ppa'] = sourceppa_info
                    self.log ("sourceppa: ... invalidating %s due to the above (ppa: %s)" % (friend_exc.name, shortppa))
                    friend_exc.addhtml("Grouped with PPA %s" % shortppa)
            return PolicyVerdict.REJECTED_PERMANENTLY
        return PolicyVerdict.PASS

    def save_state(self, britney):
        """Write source ppa data to disk"""
        tmp = self.filename + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as data:
            json.dump(self.source_ppas_by_pkg, data)
        os.rename(tmp, self.filename)
        self.log("Wrote source ppa data to %s" % self.filename)
