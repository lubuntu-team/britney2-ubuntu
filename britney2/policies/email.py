import os
import re
import json
import socket
import smtplib

from urllib.parse import unquote
from collections import defaultdict

from britney2.policies.rest import Rest
from britney2.policies.policy import BasePolicy, PolicyVerdict


API_PREFIX = 'https://api.launchpad.net/1.0/'
USER = API_PREFIX + '~'

# Don't send emails to these bots
BOTS = {
    USER + 'ci-train-bot',
    USER + 'bileto-bot',
    USER + 'ubuntu-archive-robot',
    USER + 'katie',
}

MESSAGE = """From: Ubuntu Release Team <noreply@canonical.com>
To: {recipients}
X-Proposed-Migration: notice
Subject: [proposed-migration] {source_name} {version} stuck in {series}-proposed

Hi,

{source_name} {version} needs attention.

It has been stuck in {series}-proposed for {age} day{plural}.

You either sponsored or uploaded this package, please investigate why it hasn't been approved for migration.

http://people.canonical.com/~ubuntu-archive/proposed-migration/{series}/update_excuses.html#{source_name}

https://wiki.ubuntu.com/ProposedMigration

If you have any questions about this email, please ask them in #ubuntu-release channel on Freenode IRC.

Regards, Ubuntu Release Team.
"""


def person_chooser(source):
    """Assign blame for the current source package."""
    people = {
        source['package_signer_link'],
        source['sponsor_link'],
        source['creator_link'],
    } - {None} - BOTS
    # some bots (e.g. bileto) generate uploads that are otherwise manual. We
    # want to email the people that the bot was acting on behalf of.
    bot = source['package_signer_link'] in BOTS
    # direct uploads
    regular = not source['creator_link'] and not source['sponsor_link']
    if bot or regular:
        people.add(source['package_creator_link'])
    return people


def address_chooser(addresses):
    """Prefer @ubuntu and @canonical addresses."""
    first = ''
    canonical = ''
    for address in addresses:
        if address.endswith('@ubuntu.com'):
            return address
        if address.endswith('@canonical.com'):
            canonical = address
        if not first:
            first = address
    return canonical or first


class EmailPolicy(BasePolicy, Rest):
    """Send an email when a package has been rejected."""

    def __init__(self, options, suite_info, dry_run=False):
        super().__init__('email', options, suite_info, {'unstable'})
        self.filename = os.path.join(options.unstable, 'EmailCache')
        # Dict of dicts; maps pkg name -> pkg version -> boolean
        self.emails_by_pkg = defaultdict(dict)
        # self.cache contains self.emails_by_pkg from previous run
        self.cache = {}
        self.dry_run = dry_run

    def initialise(self, britney):
        """Load cached source ppa data"""
        super().initialise(britney)

        if os.path.exists(self.filename):
            with open(self.filename, encoding='utf-8') as data:
                self.cache = json.load(data)
            self.log("Loaded cached email data from %s" % self.filename)

    def _scrape_gpg_emails(self, person):
        """Find email addresses from one person's GPG keys."""
        addresses = []
        gpg = self.query_lp_rest_api(person + '/gpg_keys', {})
        for key in gpg['entries']:
            details = self.query_rest_api('http://keyserver.ubuntu.com/pks/lookup', {
                'op': 'index',
                'search': '0x' + key['fingerprint'],
                'exact': 'on',
                'options': 'mr',
            })
            for line in details.splitlines():
                parts = line.split(':')
                if parts[0] == 'info':
                    assert int(parts[1]) == 1  # Version
                    assert int(parts[2]) <= 1  # Count
                if parts[0] == 'uid':
                    flags = parts[4]
                    if 'e' in flags or 'r' in flags:
                        continue
                    uid = unquote(parts[1])
                    match = re.match(r'^.*<(.+@.+)>$', uid)
                    if match:
                        addresses.append(match.group(1))
        return addresses

    def scrape_gpg_emails(self, people):
        """Find email addresses from GPG keys."""
        addresses = []
        for person in people or []:
            address = address_chooser(self._scrape_gpg_emails(person))
            addresses.append(address)
        return addresses

    def lp_get_emails(self, pkg, version):
        """Ask LP who uploaded this package."""
        data = self.query_lp_rest_api('%s/+archive/primary' % self.options.distribution, {
            'ws.op': 'getPublishedSources',
            'distro_series': '/%s/%s' % (self.options.distribution, self.options.series),
            'exact_match': 'true',
            'order_by_date': 'true',
            'pocket': 'Proposed',
            'source_name': pkg,
            'version': version,
        })
        try:
            source = data['entries'][0]
        # IndexError means no packages in -proposed matched this name/version,
        # which is expected to happen when bileto runs britney.
        except IndexError:
            self.log('Email getPublishedSources IndexError (%s %s)' % (pkg, version))
            return []
        return self.scrape_gpg_emails(person_chooser(source))

    def apply_policy_impl(self, email_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        """Send email if package is rejected."""
        # Have more patience for things blocked in update_output.txt
        max_age = 5 if excuse.is_valid else 1
        series = self.options.series
        version = source_data_srcdist.version
        sent = self.cache.get(source_name, {}).get(version, False)
        age = excuse.daysold or 0
        stuck = age >= max_age
        plural = 's' if age != 1 else ''
        if self.dry_run:
            self.log("[email dry run] Considering: %s/%s: %s" %
                     (source_name, version, "stuck" if stuck else "not stuck"))
            if stuck:
                self.log("[email dry run] Age %d >= threshold %d: would email: %s" %
                         (age, max_age, self.lp_get_emails(source_name, version)))
            # don't update the cache file in dry run mode; we'll see all output each time
            return PolicyVerdict.PASS
        if stuck and not sent:
            emails = self.lp_get_emails(source_name, version)
            if emails:
                recipients = ', '.join(emails)
                msg = MESSAGE.format(**locals())
                try:
                    self.log("%s/%s stuck for %d days, emailing %s" %
                              (source_name, version, age, recipients))
                    server = smtplib.SMTP('localhost')
                    server.sendmail('noreply@canonical.com', emails, msg)
                    server.quit()
                    sent = True
                except socket.error as err:
                    self.log("Failed to send mail! Is SMTP server running?")
                    self.log(err)
        self.emails_by_pkg[source_name][version] = sent
        return PolicyVerdict.PASS

    def save_state(self, britney):
        """Write source ppa data to disk"""
        tmp = self.filename + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as data:
            json.dump(self.emails_by_pkg, data)
        os.rename(tmp, self.filename)
        self.log("Wrote email data to %s" % self.filename)
