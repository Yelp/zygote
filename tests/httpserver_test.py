# -*- coding: utf-8 -*-
import testify as T

from zygote._httpserver import HTTPRequest as HTTPRequest_1
from zygote._httpserver_2 import HTTPRequest as HTTPRequest_2


class HTTPRequestReprTest(T.TestCase):

    def test_http_request_repr_does_not_show_body_or_auth_headers(self):
        self._verify_safe_repr(HTTPRequest_1)

    def test_http_request_2_repr_does_not_show_body_or_auth_headers(self):
        self._verify_safe_repr(HTTPRequest_2)

    def _verify_safe_repr(self, http_request_cls):
        request = http_request_cls(
            'POST', '/path',
            version='HTTP/1.1',
            remote_ip='127.0.0.1',
            host='127.0.0.1',
            protocol='http',
            body='sensitive post information',
            headers={'Authorization': 'Basic credentials'})

        T.assert_equal(repr(request),
            "HTTPRequest(protocol='http', host='127.0.0.1', method='POST', uri='/path', version='HTTP/1.1', remote_ip='127.0.0.1', headers={'Authorization': '***redacted***'})")


if __name__ == '__main__':
    T.run()
