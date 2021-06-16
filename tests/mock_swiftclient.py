# Mock the swiftclient Python library, the bare minimum for ADT purposes
# Author: Łukasz 'sil2100' Zemczak <lukasz.zemczak@ubuntu.com>

import os
import sys

from urllib.request import urlopen


# We want to use this single Python module file to mock out the exception
# module as well.
sys.modules["swiftclient.exceptions"] = sys.modules[__name__]


class ClientException(Exception):
    def __init__(self, msg, http_status=''):
        super(ClientException, self).__init__(msg)
        self.msg = msg
        self.http_status = http_status


class Connection:
    def __init__(self, authurl, user, key, tenant_name, auth_version):
        self._mocked_swift = 'http://localhost:18085'

    def get_container(self, container, marker=None, limit=None, prefix=None,
                      delimiter=None, end_marker=None, path=None,
                      full_listing=False, headers=None, query_string=None):
        url = os.path.join(self._mocked_swift, container) + '?' + query_string
        req = None
        try:
            req = urlopen(url, timeout=30)
            code = req.getcode()
            if code == 200:
                result_paths = req.read().decode().strip().splitlines()
            elif code == 204:  # No content
                result_paths = []
            else:
                raise ClientException('MockedError', http_status=str(code))
        except IOError as e:
            # 401 "Unauthorized" is swift's way of saying "container does not exist"
            # But here we just assume swiftclient handles this via the usual
            # ClientException.
            raise ClientException('MockedError', http_status=str(e.code) if hasattr(e, 'code') else '')
        finally:
            if req is not None:
                req.close()

        return (None, result_paths)

    def get_object(self, container, obj):
        url = os.path.join(self._mocked_swift, container, obj)
        req = None
        try:
            req = urlopen(url, timeout=30)
            code = req.getcode()
            if code == 200:
                contents = req.read()
            else:
                raise ClientException('MockedError', http_status=str(code))
        except IOError as e:
            # 401 "Unauthorized" is swift's way of saying "container does not exist"
            # But here we just assume swiftclient handles this via the usual
            # ClientException.
            raise ClientException('MockedError', http_status=str(e.code) if hasattr(e, 'code') else '')
        finally:
            if req is not None:
                req.close()

        return (None, contents)
