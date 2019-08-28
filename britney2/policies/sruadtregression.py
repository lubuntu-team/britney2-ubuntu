import os
import json
import socket
import smtplib

from collections import defaultdict
from urllib.request import urlopen, URLError

from britney2.policies.rest import Rest
from britney2.policies.policy import BasePolicy, PolicyVerdict


MESSAGE = """From: Ubuntu Stable Release Team <lukasz.zemczak@canonical.com>
To: {bug_mail}
Subject: Autopkgtest regression report ({source_name}/{version})

All autopkgtests for the newly accepted {source_name} ({version}) for {series_name} have finished running.
The following regressions have been reported in tests triggered by the package:

{failures}

Please visit the excuses page listed below and investigate the failures, proceeding afterwards as per the StableReleaseUpdates policy regarding autopkgtest regressions [1].

https://people.canonical.com/~ubuntu-archive/proposed-migration/{series_name}/update_excuses.html#{source_name}

[1] https://wiki.ubuntu.com/StableReleaseUpdates#Autopkgtest_Regressions

Thank you!
"""


class SRUADTRegressionPolicy(BasePolicy, Rest):

    def __init__(self, options, suite_info, dry_run=False):
        super().__init__('sruadtregression', options, suite_info, {'unstable'})
        self.state_filename = os.path.join(options.unstable, 'sru_regress_inform_state')
        self.state = {}
        self.dry_run = dry_run
        self.email_host = getattr(self.options, 'email_host', 'localhost')

    def initialise(self, britney):
        super().initialise(britney)
        if os.path.exists(self.state_filename):
            with open(self.state_filename, encoding='utf-8') as data:
                self.state = json.load(data)
            self.log('Loaded state file %s' % self.state_filename)
        # Remove any old entries from the statefile
        self.cleanup_state()

    def bugs_from_changes(self, change_url):
        '''Return bug list from a .changes file URL'''
        last_exception = None
        # Querying LP can timeout a lot, retry 3 times
        for i in range(3):
            try:
                changes = urlopen(change_url)
                break
            except URLError as e:
                last_exception = e
                pass
        else:
            raise last_exception
        bugs = set()
        for l in changes:
            l = l.decode('utf-8')
            if l.startswith('Launchpad-Bugs-Fixed: '):
                bugs = {int(b) for b in l.split()[1:]}
                break
        return bugs

    def apply_policy_impl(self, policy_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        # We only care about autopkgtest regressions
        if 'autopkgtest' not in excuse.reason or not excuse.reason['autopkgtest']:
            return PolicyVerdict.PASS
        # Check the policy_info to see if all tests finished and, if yes, if we
        # have any failures to report on.
        if 'autopkgtest' not in excuse.policy_info:
            return PolicyVerdict.PASS
        regressions = defaultdict(list)
        for pkg in excuse.policy_info['autopkgtest']:
            for arch in excuse.policy_info['autopkgtest'][pkg]:
                if excuse.policy_info['autopkgtest'][pkg][arch][0] == 'RUNNING':
                    # If there's at least one test still running, we just stop
                    # and not proceed any further.
                    return PolicyVerdict.PASS
                elif excuse.policy_info['autopkgtest'][pkg][arch][0] == 'REGRESSION':
                    regressions[pkg].append(arch)
        if not regressions:
            return PolicyVerdict.PASS

        version = source_data_srcdist.version
        distro_name = self.options.distribution
        series_name = self.options.series
        try:
            if self.state[distro_name][series_name][source_name] == version:
                # We already informed about the regression.
                return PolicyVerdict.PASS
        except KeyError:
            # Expected when no failure has been reported so far for this
            # source - we want to continue in that case. Easier than
            # doing n number of if-checks.
            pass
        data = self.query_lp_rest_api('%s/+archive/primary' % distro_name, {
            'ws.op': 'getPublishedSources',
            'distro_series': '/%s/%s' % (distro_name, series_name),
            'exact_match': 'true',
            'order_by_date': 'true',
            'pocket': 'Proposed',
            'source_name': source_name,
            'version': version,
        })
        try:
            src = next(iter(data['entries']))
        # IndexError means no packages in -proposed matched this name/version.
        except StopIteration:
            self.log('No packages matching %s/%s the %s/%s main archive, not '
                     'informing of ADT regressions' % (
                        source_name, version, distro_name, series_name))
            return PolicyVerdict.PASS
        changes_url = self.query_lp_rest_api(src['self_link'], {
            'ws.op': 'changesFileUrl',
        })
        if not changes_url:
            return PolicyVerdict.PASS
        # Prepare a helper string that lists all the ADT failures
        failures = ''
        for pkg, arches in regressions.items():
            failures += '%s (%s)' % (pkg, ', '.join(arches))

        bugs = self.bugs_from_changes(changes_url)
        # Now leave a comment informing about the ADT regressions on each bug
        for bug in bugs:
            self.log('%sSending ADT regression message to LP: #%s '
                     'regarding %s/%s in %s' % (
                        "[dry-run] " if self.dry_run else "", bug,
                        source_name, version, series_name))
            if not self.dry_run:
                try:
                    bug_mail = '%s@bugs.launchpad.net' % bug
                    server = smtplib.SMTP(self.email_host)
                    server.sendmail(
                        'noreply@canonical.com',
                        bug_mail,
                        MESSAGE.format(**locals()))
                    server.quit()
                except socket.error as err:
                    self.log('Failed to send mail! Is SMTP server running?')
                    self.log(err)
        self.save_progress(source_name, version, distro_name, series_name)
        return PolicyVerdict.PASS

    def save_progress(self, source, version, distro, series):
        if self.dry_run:
            return
        if distro not in self.state:
            self.state[distro] = {}
        if series not in self.state[distro]:
            self.state[distro][series] = {}
        self.state[distro][series][source] = version
        with open(self.state_filename, 'w', encoding='utf-8') as data:
            json.dump(self.state, data)

    def cleanup_state(self):
        '''Remove all no-longer-valid package entries from the statefile'''
        for distro_name in self.state:
            for series_name, pkgs in self.state[distro_name].items():
                for source_name, version in pkgs.copy().items():
                    data = self.query_lp_rest_api(
                        '%s/+archive/primary' % distro_name, {
                            'ws.op': 'getPublishedSources',
                            'distro_series': '/%s/%s' % (distro_name,
                                                         series_name),
                            'exact_match': 'true',
                            'order_by_date': 'true',
                            'pocket': 'Proposed',
                            'status': 'Published',
                            'source_name': source_name,
                            'version': version,
                        }
                    )
                    if not data['entries']:
                        del self.state[distro_name][series_name][source_name]
