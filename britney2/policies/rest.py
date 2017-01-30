import json
import socket
import urllib.request
import urllib.parse

from collections import defaultdict
from urllib.error import HTTPError


LAUNCHPAD_URL = 'https://api.launchpad.net/1.0/'


class Rest:
    """Wrap common REST APIs with some retry logic."""

    def query_rest_api(self, obj, query):
        """Do a REST request

        Request <obj>?<query>.

        Returns string received from web service.
        Raises HTTPError, ValueError, or ConnectionError based on different
        transient failures connecting.
        """

        for retry in range(5):
            url = '%s?%s' % (obj, urllib.parse.urlencode(query))
            try:
                with urllib.request.urlopen(url, timeout=30) as req:
                    code = req.getcode()
                    if 200 <= code < 300:
                        return req.read().decode('UTF-8')
                    raise ConnectionError('Failed to reach launchpad, HTTP %s'
                                          % code)
            except socket.timeout as e:
                self.log("Timeout downloading '%s', will retry %d more times."
                         % (url, 5 - retry - 1))
                exc = e
            except HTTPError as e:
                if e.code != 503:
                    raise
                self.log("Caught error 503 downloading '%s', will retry %d more times."
                         % (url, 5 - retry - 1))
                exc = e
        else:
            raise exc

    def query_lp_rest_api(self, obj, query):
        """Do a Launchpad REST request

        Request <LAUNCHPAD_URL><obj>?<query>.

        Returns dict of parsed json result from launchpad.
        Raises HTTPError, ValueError, or ConnectionError based on different
        transient failures connecting to launchpad.
        """
        if not obj.startswith(LAUNCHPAD_URL):
            obj = LAUNCHPAD_URL + obj
        return json.loads(self.query_rest_api(obj, query))
