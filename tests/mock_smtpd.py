#!/usr/bin/python3

# (C) 2017 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

from collections import defaultdict

import asyncore
import smtpd
import threading

class FakeSMTPServer(smtpd.SMTPServer):
    """A fake smtp server"""

    def __init__(self, host, port):
        # ((localhost, port), remoteaddr
        # remoteaddr is an address to relay to, which isn't relevant for us
        super().__init__((host, port), None, decode_data=False)

        # to -> (from, data)
        self.emails = defaultdict(list)

    def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
        #print('received email: %s, %s, %s' % (mailfrom, rcpttos, data))
        for rcpt in rcpttos:
            self.emails[rcpt].append(data)
        pass

    def get_emails(self):
        '''Get a list of the people that were emailed'''
        return list(self.emails.keys())

    def run(self):
        self.thread = threading.Thread(target=asyncore.loop,kwargs = {'timeout':1} )
        self.thread.start()


# support standalone running
if __name__ == "__main__":
    smtp_server = FakeSMTPServer('localhost', 1337)
    smtp_server.run()
