import os
import json
import urllib.request
import urllib.parse

from collections import defaultdict

from policies.policy import BasePolicy, PolicyVerdict


LAUNCHPAD_URL = 'https://api.launchpad.net/1.0/'
PRIMARY = LAUNCHPAD_URL + 'ubuntu/+archive/primary'
IGNORE = [
    None,
    '',
    'IndexError',
    LAUNCHPAD_URL + 'ubuntu/+archive/primary',
    LAUNCHPAD_URL + 'debian/+archive/primary',
]


def query_lp_rest_api(obj, query):
    """Do a Launchpad REST request

    Request <LAUNCHPAD_URL><obj>?<query>.

    Returns dict of parsed json result from launchpad.
    Raises HTTPError, ValueError, or ConnectionError based on different
    transient failures connecting to launchpad.
    """
    url = '%s%s?%s' % (LAUNCHPAD_URL, obj, urllib.parse.urlencode(query))
    with urllib.request.urlopen(url, timeout=30) as req:
        code = req.getcode()
        if 200 <= code < 300:
            return json.loads(req.read().decode('UTF-8'))
        raise ConnectionError('Failed to reach launchpad, HTTP %s' % code)


class SourcePPAPolicy(BasePolicy):
    """Migrate packages copied from same source PPA together

    This policy will query launchpad to determine what source PPA packages
    were copied from, and ensure that all packages from the same PPA migrate
    together.
    """

    def __init__(self, options):
        super().__init__('source-ppa', options, {'unstable'})
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

        data = query_lp_rest_api('%s/%s' % (self.options.distribution, self.options.series), {
            'ws.op': 'getPackageUploads',
            'archive': PRIMARY,
            'pocket': 'Proposed',
            'name': pkg,
            'version': version,
            'exact_match': 'true',
        })
        try:
            return data['entries'][0]['copy_source_archive_link']
        # IndexError means no packages in -proposed matched this name/version,
        # which is expected to happen when bileto runs britney.
        except IndexError:
            self.log('SourcePPA getPackageUploads IndexError (%s %s)' % (pkg, version))
            return 'IndexError'

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
        sourceppa = self.lp_get_source_ppa(source_name, version)
        self.source_ppas_by_pkg[source_name][version] = sourceppa
        if sourceppa in IGNORE:
            return PolicyVerdict.PASS

        shortppa = sourceppa.replace(LAUNCHPAD_URL, '')
        sourceppa_info[source_name] = shortppa
        # Check for other packages that might invalidate this one
        for friend in self.pkgs_by_source_ppa[sourceppa]:
            sourceppa_info[friend] = shortppa
            if not britney_excuses[friend].is_valid:
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
                    self.log("Blocking %s because %s from %s" % (friend, source_name, shortppa))
                    friend_exc.addhtml("Blocking because %s from the same PPA %s is invalid" %
                                       (friend, shortppa))
            return PolicyVerdict.REJECTED_PERMANENTLY
        return PolicyVerdict.PASS

    def save_state(self, britney):
        """Write source ppa data to disk"""
        tmp = self.filename + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as data:
            json.dump(self.source_ppas_by_pkg, data)
        os.rename(tmp, self.filename)
        self.log("Wrote source ppa data to %s" % self.filename)
