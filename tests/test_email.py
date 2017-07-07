#!/usr/bin/python3
# (C) 2017 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

from collections import defaultdict

import fileinput
import json
import os
import pprint
import sys
import unittest
import yaml
from unittest.mock import DEFAULT, patch, call

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from britney2.policies.policy import PolicyVerdict
from britney2.policies.email import EmailPolicy, person_chooser, address_chooser

from tests.test_sourceppa import FakeOptions
from tests import TestBase
from tests.mock_smtpd import FakeSMTPServer

# Example of a direct upload by core dev: openstack-doc-tools 1.5.0-0ubuntu1
# https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/7524835
UPLOAD = dict(
    creator_link=None,
    package_creator_link='https://api.launchpad.net/1.0/~zulcss',
    package_signer_link='https://api.launchpad.net/1.0/~zulcss',
    sponsor_link=None,
)

# Example of a sponsored upload: kerneloops 0.12+git20140509-2ubuntu1
# https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/7013597
SPONSORED_UPLOAD = dict(
    creator_link=None,
    package_creator_link='https://api.launchpad.net/1.0/~smb',
    package_signer_link='https://api.launchpad.net/1.0/~apw',
    sponsor_link=None,
)

# Example of a bileto upload: autopilot 1.6.0+17.04.20170302-0ubuntu1
# https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/7525085
# (dobey clicked 'build' and sil2100 clicked 'publish')
BILETO = dict(
    creator_link='https://api.launchpad.net/1.0/~sil2100',
    package_creator_link='https://api.launchpad.net/1.0/~dobey',
    package_signer_link='https://api.launchpad.net/1.0/~ci-train-bot',
    sponsor_link='https://api.launchpad.net/1.0/~ubuntu-archive-robot',
)

# Example of a non-sponsored copy: linux 4.10.0-11.13
# https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/7522481
# (the upload to the PPA was sponsored but the copy was done directly)
UNSPONSORED_COPY = dict(
    creator_link='https://api.launchpad.net/1.0/~timg-tpi',
    package_creator_link='https://api.launchpad.net/1.0/~sforshee',
    package_signer_link='https://api.launchpad.net/1.0/~timg-tpi',
    sponsor_link=None,
)

# Example of a sponsored copy: pagein 0.00.03-1
# https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/7533336
SPONSORED_COPY = dict(
    creator_link='https://api.launchpad.net/1.0/~colin-king',
    package_creator_link='https://api.launchpad.net/1.0/~colin-king',
    package_signer_link=None,
    sponsor_link='https://api.launchpad.net/1.0/~mapreri',
)

# Example of a manual debian sync: systemd 232-19
# https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/7522736
MANUAL_SYNC = dict(
    creator_link='https://api.launchpad.net/1.0/~costamagnagianfranco',
    package_creator_link='https://api.launchpad.net/1.0/~pkg-systemd-maintainers',
    package_signer_link=None,
    sponsor_link=None,
)

# Example of a sponsored manual debian sync: python-pymysql 0.7.9-2
# https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/7487820
SPONSORED_MANUAL_SYNC = dict(
    creator_link='https://api.launchpad.net/1.0/~lars-tangvald',
    package_creator_link='https://api.launchpad.net/1.0/~openstack-1.0',
    package_signer_link=None,
    sponsor_link='https://api.launchpad.net/1.0/~racb',
)

# Example of an automatic debian sync: gem2deb 0.33.1
# https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/7255529
AUTO_SYNC = dict(
    creator_link='https://api.launchpad.net/1.0/~katie',
    package_creator_link='https://api.launchpad.net/1.0/~pkg-ruby-extras-maintainers',
    package_signer_link=None,
    sponsor_link='https://api.launchpad.net/1.0/~ubuntu-archive-robot',
)


# address lists
UBUNTU = ['personal@gmail.com', 'ubuntu@ubuntu.com', 'work@canonical.com']
CANONICAL = ['personal@gmail.com', 'work@canonical.com']
COMMUNITY = ['personal@gmail.com', 'other@gmail.com']


def retvals(retvals):
    """Return different retvals on different calls of mock."""
    def returner(*args, **kwargs):
        return retvals.pop()
    return returner


class FakeSourceData:
    version = '55.0'


class FakeExcuse:
    is_valid = True
    daysold = 0
    reason = []
    current_policy_verdict = PolicyVerdict.PASS


class T(unittest.TestCase):
    maxDiff = None

    def test_person_chooser(self):
        """Find the correct person to blame for an upload."""
        self.assertEqual(person_chooser(UPLOAD), {
            'https://api.launchpad.net/1.0/~zulcss',
        })
        self.assertEqual(person_chooser(SPONSORED_UPLOAD), {
            'https://api.launchpad.net/1.0/~apw',
            'https://api.launchpad.net/1.0/~smb'
        })
        self.assertEqual(person_chooser(BILETO), {
            'https://api.launchpad.net/1.0/~dobey',
            'https://api.launchpad.net/1.0/~sil2100',
        })
        self.assertEqual(person_chooser(UNSPONSORED_COPY), {
            'https://api.launchpad.net/1.0/~timg-tpi',
        })
        self.assertEqual(person_chooser(SPONSORED_COPY), {
            'https://api.launchpad.net/1.0/~colin-king',
            'https://api.launchpad.net/1.0/~mapreri',
        })
        self.assertEqual(person_chooser(MANUAL_SYNC), {
            'https://api.launchpad.net/1.0/~costamagnagianfranco',
        })
        self.assertSequenceEqual(person_chooser(SPONSORED_MANUAL_SYNC), {
            'https://api.launchpad.net/1.0/~lars-tangvald',
            'https://api.launchpad.net/1.0/~racb',
        })
        self.assertEqual(person_chooser(AUTO_SYNC), set())

    def test_address_chooser(self):
        """Prioritize email addresses correctly."""
        self.assertEqual(address_chooser(UBUNTU), 'ubuntu@ubuntu.com')
        self.assertEqual(address_chooser(CANONICAL), 'work@canonical.com')
        self.assertEqual(address_chooser(COMMUNITY), 'personal@gmail.com')

    @patch('britney2.policies.email.EmailPolicy.query_rest_api')
    @patch('britney2.policies.email.EmailPolicy.query_lp_rest_api')
    def test_email_scraping(self, lp, rest):
        """Poke correct REST APIs to find email addresses."""
        lp.side_effect = retvals([
            dict(entries=[dict(fingerprint='DEFACED_ED1F1CE')]),
            dict(entries=[UPLOAD]),
        ])
        rest.return_value = 'uid:Defaced Edifice <ex@example.com>:12345::'
        e = EmailPolicy(FakeOptions, None)
        self.assertEqual(e.lp_get_emails('openstack-doct-tools', '1.5.0-0ubuntu1'), ['ex@example.com'])
        self.assertSequenceEqual(lp.mock_calls, [
            call('testbuntu/+archive/primary', {
                'distro_series': '/testbuntu/zazzy',
                'exact_match': 'true',
                'order_by_date': 'true',
                'pocket': 'Proposed',
                'source_name': 'openstack-doct-tools',
                'version': '1.5.0-0ubuntu1',
                'ws.op': 'getPublishedSources',
            }),
            call('https://api.launchpad.net/1.0/~zulcss/gpg_keys', {})
        ])
        self.assertSequenceEqual(rest.mock_calls, [
            call('http://keyserver.ubuntu.com/pks/lookup', {
                'exact': 'on',
                'op': 'index',
                'options': 'mr',
                'search': '0xDEFACED_ED1F1CE',
            })
        ])

    @patch('britney2.policies.email.EmailPolicy.lp_get_emails')
    @patch('britney2.policies.email.smtplib')
    def test_smtp_not_sent(self, smtp, lp):
        """Know when not to send any emails."""
        lp.return_value = ['example@email.com']
        e = EmailPolicy(FakeOptions, None)
        FakeExcuse.daysold = 0.002
        e.apply_policy_impl(None, None, 'chromium-browser', None, FakeSourceData, FakeExcuse)
        FakeExcuse.daysold = 2.98
        e.apply_policy_impl(None, None, 'chromium-browser', None, FakeSourceData, FakeExcuse)
        # Would email but no address found
        FakeExcuse.daysold = 10.12
        lp.return_value = []
        e.apply_policy_impl(None, None, 'chromium-browser', None, FakeSourceData, FakeExcuse)
        self.assertEqual(smtp.mock_calls, [])

    @patch('britney2.policies.email.EmailPolicy.lp_get_emails')
    @patch('britney2.policies.email.smtplib')
    def test_smtp_sent(self, smtp, lp):
        """Send emails correctly."""
        lp.return_value = ['email@address.com']
        e = EmailPolicy(FakeOptions, None)
        FakeExcuse.is_valid = False
        FakeExcuse.daysold = 100
        e.apply_policy_impl(None, None, 'chromium-browser', None, FakeSourceData, FakeExcuse)
        smtp.SMTP.assert_called_once_with('localhost')

    @patch('britney2.policies.email.EmailPolicy.lp_get_emails')
    @patch('britney2.policies.email.smtplib', autospec=True)
    def smtp_repetition(self, smtp, lp, valid, expected):
        """Resend mails periodically, with decreasing frequency."""
        if not isinstance(valid,list):
            valid = [valid]*len(expected)
        FakeExcuse.is_valid = valid
        lp.return_value = ['email@address.com']
        sendmail = smtp.SMTP().sendmail
        e = EmailPolicy(FakeOptions, None)
        called = []
        e.cache = {}
        for hours in range(0, 5000):
            previous = sendmail.call_count
            age = hours / 24
            FakeExcuse.daysold = age
            try:
                FakeExcuse.is_valid = valid[len(called)]
            except IndexError:
                # we've already gotten all the mails we expect
                pass
            e.apply_policy_impl(None, None, 'unity8', None, FakeSourceData, FakeExcuse)
            if sendmail.call_count > previous:
                e.initialise(None)  # Refill e.cache from disk
                called.append(age)
                name, args, kwargs = sendmail.mock_calls[-1]
                text = args[2]
                self.assertNotIn(' 1 days.', text)
        self.assertSequenceEqual(called, expected)

    def test_smtp_repetition(self):
        """Confirm that emails are sent at appropriate intervals."""
        # Emails were sent when daysold reached these values:
        self.smtp_repetition(valid=False, expected=[
            1, 3, 7, 15, 31, 61, 91, 121, 151, 181
        ])
        self.smtp_repetition(valid=True, expected=[
            5, 7, 11, 19, 35, 65, 95, 125, 155, 185
        ])
        self.smtp_repetition(valid=[False, False, True], expected=[
            1, 3, 5, 7, 11, 19, 35, 65, 95, 125, 155, 185
        ])
        self.smtp_repetition(valid=[False, False, True, False, True], expected=[
            1, 3, 5, 7, 11, 19, 35, 65, 95, 125, 155, 185
        ])


class ET(TestBase):
    ''' Test sending mail through a mocked SMTP server '''
    @classmethod
    def setUpClass(cls):
        cls.smtpd = FakeSMTPServer('localhost', 1337)
        cls.smtpd.run()

    @classmethod
    def tearDownClass(cls):
        cls.smtpd.close()

    def setUp(self):
        super().setUp()
        # disable ADT, not relevant for us
        for line in fileinput.input(self.britney_conf, inplace=True):
            if line.startswith('ADT_ENABLE'):
                print('ADT_ENABLE = no')
            elif line.startswith('MINDAYS_EMERGENCY'):
                print('MINDAYS_EMERGENCY = 10')
            elif not line.startswith('ADT_AMQP') and not line.startswith('ADT_SWIFT_URL'):
                sys.stdout.write(line)
        # and set up a fake smtpd
        with open(self.britney_conf, 'a') as f:
            f.write('EMAIL_HOST = localhost:1337')
        self.sourceppa_cache = {}
        self.email_cache = {}

        self.data.add('libc6', False)

    def do_test(self, unstable_add, expect_emails):
        '''Run britney with some unstable packages and verify excuses.

        unstable_add is a list of (binpkgname, field_dict, daysold, emails)

        expect_emails is a list that is checked against the emails sent during
        this do_test run.

        Return (output, excuses_dict, excuses_html, emails).
        '''
        ET.smtpd.emails.clear()
        age_file = os.path.join(self.data.path,
                                'data',
                                'series',
                                'Dates')
        email_cache_file = os.path.join(self.data.path,
                                        'data',
                                        'series-proposed',
                                        'EmailCache')
        for (pkg, fields, daysold, emails) in unstable_add:
            self.data.add(pkg, True, fields, True, None)
            self.sourceppa_cache.setdefault(pkg, {})
            if fields['Version'] not in self.sourceppa_cache[pkg]:
                self.sourceppa_cache[pkg][fields['Version']] = ''
            with open(age_file, 'w') as f:
                import time
                do = time.time() - (60 * 60 * 24 * daysold)
                f.write('%s %s %d' % (pkg, fields['Version'], do))

            with open(email_cache_file, 'w') as f:
                d = defaultdict(dict)
                d[pkg][fields['Version']] = (emails, 0)
                f.write(json.dumps(d))

        # Set up sourceppa cache for testing
        sourceppa_path = os.path.join(self.data.dirs[True], 'SourcePPA')
        with open(sourceppa_path, 'w', encoding='utf-8') as sourceppa:
            sourceppa.write(json.dumps(self.sourceppa_cache))

        (excuses_yaml, excuses_html, out) = self.run_britney()

        # convert excuses to source indexed dict
        excuses_dict = {}
        for s in yaml.load(excuses_yaml)['sources']:
            excuses_dict[s['source']] = s

        if 'SHOW_EXCUSES' in os.environ:
            print('------- excuses -----')
            pprint.pprint(excuses_dict, width=200)
        if 'SHOW_HTML' in os.environ:
            print('------- excuses.html -----\n%s\n' % excuses_html)
        if 'SHOW_OUTPUT' in os.environ:
            print('------- output -----\n%s\n' % out)

        self.assertNotIn('FIXME', out)
        # check all the emails that we asked for are there
        for email in expect_emails:
            self.assertIn(email, ET.smtpd.get_emails())
        self.assertEqual(len(ET.smtpd.get_emails()), len(expect_emails))

        return (out, excuses_dict, excuses_html, ET.smtpd.emails)

    def test_email_sent(self):
        '''Test that an email is sent through the SMTP server'''
        pkg = ('libc6', {'Version': '2',
                         'Depends': 'notavailable (>= 2)'},
               6,
               ['foo@bar.com'])

        self.do_test([pkg], ["foo@bar.com"])

    def test_email_not_sent_block_all_source(self):
        '''Test that an email is not sent if the package is blocked by a
           block-all source hint'''
        self.create_hint('freeze', 'block-all source')
        pkg = ('libc6', {'Version': '2'},
               6,  # daysold
               ['foo@bar.com'])

        self.do_test([pkg], [])

    def test_email_not_sent_blocked(self):
        '''Test that an email is not sent if the package is blocked by a block hint'''
        self.create_hint('freeze', 'block libc6')
        pkg = ('libc6', {'Version': '2'},
               6,  # daysold
               ['foo@bar.com'])

        self.do_test([pkg], [])

    def test_email_sent_unblocked(self):
        '''Test that an email is sent if the package is unblocked'''
        self.create_hint('freeze', 'block libc6')
        self.create_hint('laney', 'unblock libc6/2')
        pkg = ('libc6', {'Version': '2',
                         'Depends': 'notavailable (>= 2)'},
               6,  # daysold
               ['foo@bar.com'])

        self.do_test([pkg], ['foo@bar.com'])

    def test_email_not_sent_rejected_temporarily(self):
        '''Test that an email is not sent if the package is REJECTED_TEMPORARILY'''
        urgency_file = os.path.join(self.data.path,
                                    'data',
                                    'series',
                                    'Urgency')
        with open(urgency_file, 'w') as f:
            # we specified in setUp() that emergency has a 10 day delay, and
            # age rejections are REJECTED_TEMPORARILY
            f.write('libc6 2 emergency')

        pkg = ('libc6', {'Version': '2',
                         'Depends': 'notavailable (>= 2)'},
               6,  # daysold
               ['foo@bar.com'])

        self.do_test([pkg], [])


if __name__ == '__main__':
    unittest.main()
