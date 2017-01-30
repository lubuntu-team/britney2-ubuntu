import os
import re
import json
import smtplib

from urllib.parse import unquote
from collections import defaultdict
from email.mime.text import MIMEText

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

MESSAGE_BODY = """{source_name} {version} needs attention.

It has been stuck in {series}-proposed for over a day.

You either sponsored or uploaded this package, please investigate why it hasn't been approved for migration.

http://people.canonical.com/~ubuntu-archive/proposed-migration/{series}/update_excuses.html#{source_name}

https://wiki.ubuntu.com/ProposedMigration

If you have any questions about this email, please ask them in #ubuntu-release channel on Freenode IRC.
"""


def person_chooser(source):
    """Assign blame for the current source package."""
    people = {
        source['package_signer_link'],
        source['sponsor_link'],
        source['creator_link'],
    } - {None} - BOTS
    if source['package_signer_link'] in BOTS:
        people.add(source['package_creator_link'])
    return people


def address_chooser(addresses):
    """Prefer @ubuntu and @canonical addresses."""
    first = None
    canonical = None
    for address in addresses:
        if address.endswith('@ubuntu.com'):
            return address
        if address.endswith('@canonical.com'):
            canonical = address
        if first is None:
            first = address
    return canonical or first


class EmailPolicy(BasePolicy, Rest):
    """Send an email when a package has been rejected."""

    def __init__(self, options, suite_info):
        super().__init__('email', options, suite_info, {'unstable'})
        self.filename = os.path.join(options.unstable, 'EmailCache')
        # Dict of dicts; maps pkg name -> pkg version -> boolean
        self.emails_by_pkg = defaultdict(dict)
        # self.cache contains self.emails_by_pkg from previous run
        self.cache = {}

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
            return None
        return self.scrape_gpg_emails(person_chooser(source))

    def apply_policy_impl(self, email_info, suite, source_name, source_data_tdist, source_data_srcdist, excuse):
        """Send email if package is rejected."""
        # Have more patience for things blocked in update_output.txt
        max_age = 5 if excuse.is_valid else 1
        series = self.options.series
        version = source_data_srcdist.version
        sent = self.cache.get(source_name, {}).get(version, False)
        stuck = (excuse.daysold or 0) >= max_age
        if stuck and not sent:
            msg = MIMEText(MESSAGE_BODY.format(**locals()))
            msg['X-Proposed-Migration'] = 'notice'
            msg['Subject'] = '[proposed-migration] {} {} stuck in {}-proposed'.format(source_name, version, series)
            msg['From'] = 'noreply@canonical.com'
            emails = self.lp_get_emails(source_name, version)
            if emails:
                msg['To'] = ', '.join(emails)
                try:
                    with smtplib.SMTP('localhost') as smtp:
                        smtp.send_message(msg)
                        sent = True
                except ConnectionRefusedError as err:
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
