import ssl
from urllib.error import URLError
from urllib.request import Request

import pytest

from opportunity_agent.modelscope_transport import open_modelscope_request


def test_ssl_eof_retries_once_with_verified_tls12_context():
    calls = []
    eof = URLError(ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "unexpected eof"))

    def opener(_request, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise eof
        return "ok"

    result = open_modelscope_request(Request("https://example.invalid"), 10, opener=opener)
    assert result == "ok"
    assert "context" not in calls[0]
    assert calls[1]["context"].minimum_version == ssl.TLSVersion.TLSv1_2
    assert calls[1]["context"].maximum_version == ssl.TLSVersion.TLSv1_2


def test_non_tls_error_is_not_retried():
    calls = []

    def opener(_request, **kwargs):
        calls.append(kwargs)
        raise URLError("network unreachable")

    with pytest.raises(URLError):
        open_modelscope_request(Request("https://example.invalid"), 10, opener=opener)
    assert len(calls) == 1
