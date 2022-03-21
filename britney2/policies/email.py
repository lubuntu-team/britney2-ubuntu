import os
import re
import json
import math
import socket
import smtplib

from urllib.error import HTTPError
from urllib.parse import unquote
from collections import defaultdict

from britney2 import SuiteClass
from britney2.policies.rest import Rest
from britney2.policies.policy import BasePolicy, PolicyVerdict


# Recurring emails should never be more than this many days apart
MAX_INTERVAL = 30

API_PREFIX = "https://api.launchpad.net/1.0/"
USER = API_PREFIX + "~"

# Don't send emails to these bots
BOTS = {
    USER + "ci-train-bot",
    USER + "bileto-bot",
    USER + "ubuntu-archive-robot",
    USER + "katie",
}

MESSAGE = """From: Ubuntu Release Team <noreply+proposed-migration@ubuntu.com>
To: {recipients}
X-Proposed-Migration: notice
Subject: [proposed-migration] {source_name} {version} stuck in {series}-proposed for {age} day{plural}.

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
    people = (
        {
            source["package_signer_link"],
            source["sponsor_link"],
            source["creator_link"],
        }
        - {None}
        - BOTS
    )
    # some bots (e.g. bileto) generate uploads that are otherwise manual. We
    # want to email the people that the bot was acting on behalf of.
    bot = source["package_signer_link"] in BOTS
    # direct uploads
    regular = not source["creator_link"] and not source["sponsor_link"]
    if bot or regular:
        people.add(source["package_creator_link"])
    return people


def address_chooser(addresses):
    """Prefer @ubuntu and @canonical addresses."""
    first = ""
    canonical = ""
    for address in addresses:
        if address.endswith("@ubuntu.com"):
            return address
        if address.endswith("@canonical.com"):
            canonical = address
        if not first:
            first = address
    return canonical or first


class EmailPolicy(BasePolicy, Rest):
    """Send an email when a package has been rejected."""

    def __init__(self, options, suite_info, dry_run=False):
        super().__init__(
            "email", options, suite_info, {SuiteClass.PRIMARY_SOURCE_SUITE}
        )
        self.filename = os.path.join(options.unstable, "EmailCache")
        # Maps lp username -> email address
        self.addresses = {}
        # Dict of dicts; maps pkg name -> pkg version -> boolean
        self.emails_by_pkg = defaultdict(dict)
        # self.cache contains self.emails_by_pkg from previous run
        self.cache = {}
        self.dry_run = dry_run
        self.email_host = getattr(self.options, "email_host", "localhost")
        self.logger.info(
            "EmailPolicy: will send emails to: %s", self.email_host
        )

    def initialise(self, britney):
        """Load cached source ppa data"""
        super().initialise(britney)

        if os.path.exists(self.filename):
            with open(self.filename, encoding="utf-8") as data:
                self.cache = json.load(data)
            self.logger.info("Loaded cached email data from %s" % self.filename)
        tmp = self.filename + ".new"
        if os.path.exists(tmp):
            # if we find a record on disk of emails sent from an incomplete
            # britney run, merge them in now.
            with open(tmp, encoding="utf-8") as data:
                self.cache.update(json.load(data))
            self._save_progress(self.cache)
            self.save_state()

    def _scrape_gpg_emails(self, person):
        """Find email addresses from one person's GPG keys."""
        if person in self.addresses:
            return self.addresses[person]
        addresses = []
        try:
            gpg = self.query_lp_rest_api(person + "/gpg_keys", {})
            for key in gpg["entries"]:
                details = self.query_rest_api(
                    "http://keyserver.ubuntu.com/pks/lookup",
                    {
                        "op": "index",
                        "search": "0x" + key["fingerprint"],
                        "exact": "on",
                        "options": "mr",
                    },
                )
                for line in details.splitlines():
                    parts = line.split(":")
                    if parts[0] == "info":
                        if int(parts[1]) != 1 or int(parts[2]) > 1:
                            break
                    if parts[0] == "uid":
                        flags = parts[4]
                        if "e" in flags or "r" in flags:
                            continue
                        uid = unquote(parts[1])
                        match = re.match(r"^.*<(.+@.+)>$", uid)
                        if match:
                            addresses.append(match.group(1))
            address = self.addresses[person] = address_chooser(addresses)
            return address
        except HTTPError as e:
            if e.code != 410:  # suspended user
                raise
            self.logger.info(
                "Ignoring person %s as suspended in Launchpad" % person
            )
            return None

    def scrape_gpg_emails(self, people):
        """Find email addresses from GPG keys."""
        emails = [self._scrape_gpg_emails(person) for person in (people or [])]
        return [email for email in emails if email is not None]

    def lp_get_emails(self, pkg, version):
        """Ask LP who uploaded this package."""
        try:
            data = self.query_lp_rest_api(
                "%s/+archive/primary" % self.options.distribution,
                {
                    "ws.op": "getPublishedSources",
                    "distro_series": "/%s/%s"
                    % (self.options.distribution, self.options.series),
                    "exact_match": "true",
                    "order_by_date": "true",
                    "pocket": "Proposed",
                    "source_name": pkg,
                    "version": version,
                },
            )
        except urllib.error.URLError as e:
            self.logger.error("Error getting uploader from Launchpad for %s/%s: %s",
                              pkg, version, e.reason)
        try:
            source = next(reversed(data["entries"]))
        # IndexError means no packages in -proposed matched this name/version,
        # which is expected to happen when bileto runs britney.
        except StopIteration:
            self.logger.info(
                "Email getPublishedSources IndexError (%s %s)" % (pkg, version)
            )
            return []
        return self.scrape_gpg_emails(person_chooser(source))

    def apply_src_policy_impl(
        self, email_info, item, source_data_tdist, source_data_srcdist, excuse
    ):
        """Send email if package is rejected."""
        source_name = item.package
        max_age = 5 if excuse.is_valid else 1
        series = self.options.series
        version = source_data_srcdist.version
        age = int(excuse.daysold) or 0
        plural = "" if age == 1 else "s"
        # an item is stuck if it's
        # - old enough
        # - not blocked
        # - not temporarily rejected (e.g. by the autopkgtest policy when tests
        #   are still running)
        stuck = (
            age >= max_age
            and "block" not in excuse.reason
            and excuse.tentative_policy_verdict
            != PolicyVerdict.REJECTED_TEMPORARILY
        )
        if self.dry_run:
            self.logger.info(
                "[email dry run] Considering: %s/%s: %s"
                % (source_name, version, "stuck" if stuck else "not stuck")
            )

        if not stuck:
            return PolicyVerdict.PASS

        cached = self.cache.get(source_name, {}).get(version)
        try:
            emails, last_sent = cached
            # migration of older data
            last_sent = int(last_sent)
            # Find out whether we are due to send another email by calculating
            # the most recent age at which we should have sent one.  A
            # sequence of doubling intervals (0 + 1 = 1, 1 + 2 = 3, 3 + 4 = 7)
            # is equivalent to 2^n-1, or 2^n + (max_age - 1) - 1.
            # 2^(floor(log2(age))) straightforwardly calculates the most
            # recent age at which we wanted to send an email.
            last_due = int(
                math.pow(2, int(math.log(age + 2 - max_age, 2))) + max_age - 2
            )
            # Don't let the interval double without bounds.
            if last_due - max_age >= MAX_INTERVAL:
                last_due = (
                    int((age - max_age - MAX_INTERVAL) / MAX_INTERVAL)
                    * MAX_INTERVAL
                    + max_age
                    + MAX_INTERVAL
                )
            # And don't send emails before we've reached the minimum age
            # threshold.
            if last_due < max_age:
                last_due = max_age

        except TypeError:
            # This exception happens when source_name, version never seen before
            emails = []
            last_sent = 0
            last_due = max_age
        if self.dry_run:
            self.logger.info(
                "[email dry run] Age %d >= threshold %d: would email: %s"
                % (age, max_age, self.lp_get_emails(source_name, version))
            )
            # don't update the cache file in dry run mode; we'll see all output each time
            return PolicyVerdict.PASS
        if last_sent < last_due:
            if not emails:
                emails = self.lp_get_emails(source_name, version)
            if emails:
                recipients = ", ".join(emails)
                msg = MESSAGE.format(**locals())
                try:
                    self.logger.info(
                        "%s/%s stuck for %d days (email last sent at %d days old, "
                        "threshold for sending %d days), emailing %s"
                        % (
                            source_name,
                            version,
                            age,
                            last_sent,
                            last_due,
                            recipients,
                        )
                    )
                    server = smtplib.SMTP(self.email_host)
                    server.sendmail("noreply+proposed-migration@ubuntu.com", emails, msg)
                    server.quit()
                    # record the age at which the mail should have been sent
                    last_sent = last_due
                except socket.error as err:
                    self.logger.error(
                        "Failed to send mail! Is SMTP server running?"
                    )
                    self.logger.error(err)
        self.emails_by_pkg[source_name][version] = (emails, last_sent)
        self._save_progress(self.emails_by_pkg)
        return PolicyVerdict.PASS

    def _save_progress(self, my_data):
        """Checkpoint after each sent mail"""
        tmp = self.filename + ".new"
        with open(tmp, "w", encoding="utf-8") as data:
            json.dump(my_data, data)
        return tmp

    def save_state(self, britney=None):
        """Save email notification status of all pending packages"""
        if not self.dry_run:
            try:
                os.rename(self.filename + ".new", self.filename)
            # if we haven't written any cache, don't clobber the old one
            except FileNotFoundError:
                pass
        if britney:
            self.logger.info("Wrote email data to %s" % self.filename)
