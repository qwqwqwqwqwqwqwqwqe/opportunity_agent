"""Small, security-preserving TLS compatibility shim for ModelScope calls."""

from __future__ import annotations

import ssl
import errno
import json
import logging
import socket
import time
from collections.abc import Callable
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen, getproxies, proxy_bypass

log = logging.getLogger(__name__)


def transport_diagnostic(error, url=''):
    """Classify observable failures, never attribute an ambiguous EOF to a server."""
    host = urlsplit(url).hostname or ''
    proxy = getproxies().get(urlsplit(url).scheme, '') if host and not proxy_bypass(host) else ''
    reason = getattr(error, 'reason', error)
    code, layer, certainty = 'TRANSPORT_UNKNOWN', 'unknown', 'undetermined'
    if isinstance(error, HTTPError):
        code, layer, certainty = 'HTTP_'+str(error.code), 'http_response', 'observed'
        if error.code == 407: code, layer = 'PROXY_AUTH_REQUIRED', 'proxy'
    elif isinstance(reason, ssl.SSLCertVerificationError):
        code, layer, certainty = 'TLS_CERTIFICATE_VERIFY_FAILED', 'tls_verification', 'observed'
    elif isinstance(reason, ssl.SSLError):
        code, layer = ('TLS_UNEXPECTED_EOF' if _is_handshake_eof(URLError(reason)) else 'TLS_HANDSHAKE_FAILED'), 'tls_path'
    elif isinstance(reason, socket.gaierror):
        code, layer, certainty = 'DNS_RESOLUTION_FAILED', 'proxy_dns' if proxy else 'destination_dns', 'observed'
    elif isinstance(reason, (TimeoutError, socket.timeout)):
        code, layer = 'NETWORK_TIMEOUT', 'network_path'
    elif isinstance(reason, OSError):
        code = {errno.ECONNREFUSED:'CONNECTION_REFUSED', errno.ENETUNREACH:'NETWORK_UNREACHABLE', errno.EHOSTUNREACH:'HOST_UNREACHABLE', errno.ECONNRESET:'CONNECTION_RESET'}.get(reason.errno,'NETWORK_IO_ERROR')
        layer = 'proxy_connection' if proxy else 'network_path'
    return {'code':code,'layer':layer,'certainty':certainty,'destination_host':host,
            'proxy_configured':bool(proxy),'proxy_host':urlsplit(proxy).hostname or '',
            'exception_type':type(error).__name__,'reason_type':type(reason).__name__}


def transport_failure_message(error):
    d = transport_diagnostic(error)
    messages = {'TLS_UNEXPECTED_EOF':'模型TLS连接被提前断开，尚不能确定是代理、中间网络还是服务端',
        'TLS_CERTIFICATE_VERIFY_FAILED':'模型服务证书验证失败（未绕过验证）',
        'DNS_RESOLUTION_FAILED':'模型服务或代理域名解析失败', 'PROXY_AUTH_REQUIRED':'代理要求认证',
        'NETWORK_TIMEOUT':'模型网络连接或读取超时','CONNECTION_REFUSED':'模型服务或代理连接被拒绝'}
    return d['code']+'：'+messages.get(d['code'],'模型服务请求失败，请查看脱敏网络诊断日志')


def open_modelscope_request(
    request: Request,
    timeout: int,
    opener: Callable[..., Any] = urlopen,
    retry_handshake: bool = True,
) -> Any:
    """Open normally, retrying once with TLS 1.2 only after a handshake EOF.

    Some Windows network appliances close a TLS 1.3 ClientHello to this host.
    Certificate verification remains enabled. Timeouts and all other transport
    errors are deliberately not retried.
    """
    started = time.monotonic()
    try:
        return opener(request, timeout=timeout)
    except (URLError, OSError) as exc:
        diagnostic = transport_diagnostic(exc, request.full_url)
        diagnostic.update(event='model_transport_failure',elapsed_ms=int((time.monotonic()-started)*1000),tls_profile='default')
        log.warning('%s',json.dumps(diagnostic))
        if not retry_handshake or not _is_handshake_eof(exc):
            raise
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        remaining = timeout - (time.monotonic()-started)
        if remaining <= 0:
            raise
        try:
            result = opener(request, timeout=remaining, context=context)
            log.warning('%s',json.dumps({'event':'model_transport_recovered','destination_host':diagnostic['destination_host'],'tls_profile':'TLSv1.2'}))
            return result
        except (URLError, OSError) as retry_error:
            diagnostic = transport_diagnostic(retry_error, request.full_url)
            diagnostic.update(event='model_transport_failure',tls_profile='TLSv1.2',elapsed_ms=int((time.monotonic()-started)*1000))
            log.warning('%s',json.dumps(diagnostic))
            raise


def _is_handshake_eof(error: URLError) -> bool:
    reason = getattr(error, 'reason', error)
    return isinstance(reason, ssl.SSLEOFError) or "UNEXPECTED_EOF_WHILE_READING" in str(reason)
