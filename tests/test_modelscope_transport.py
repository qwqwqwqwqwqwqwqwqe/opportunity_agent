import ssl
from urllib.error import URLError
from urllib.request import Request

import pytest

from opportunity_agent.modelscope_transport import open_modelscope_request
from opportunity_agent.modelscope_transport import transport_diagnostic


def test_diagnostics_distinguish_observations_without_secrets(monkeypatch):
    import socket
    import opportunity_agent.modelscope_transport as transport
    monkeypatch.setattr(transport,'getproxies',lambda:{'https':'http://user:secret@proxy.example:7897'})
    monkeypatch.setattr(transport,'proxy_bypass',lambda _:False)
    d=transport_diagnostic(URLError(ssl.SSLEOFError(8,'eof')),'https://api.example/v1?key=secret')
    assert d['code']=='TLS_UNEXPECTED_EOF' and d['certainty']=='undetermined'
    assert d['proxy_host']=='proxy.example'
    assert 'secret' not in str(d) and 'user:' not in str(d)
    assert transport_diagnostic(URLError(socket.gaierror(-2,'dns')),'https://api.example')['layer']=='proxy_dns'
    assert transport_diagnostic(URLError(ssl.SSLCertVerificationError(1,'certificate')))['code']=='TLS_CERTIFICATE_VERIFY_FAILED'
    assert transport_diagnostic(URLError(TimeoutError()))['code']=='NETWORK_TIMEOUT'


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
