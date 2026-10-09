"""Small, security-preserving TLS compatibility shim for ModelScope calls."""

from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


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
    try:
        return opener(request, timeout=timeout)
    except URLError as exc:
        if not retry_handshake or not _is_handshake_eof(exc):
            raise
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        return opener(request, timeout=timeout, context=context)


def _is_handshake_eof(error: URLError) -> bool:
    reason = error.reason
    return isinstance(reason, ssl.SSLEOFError) or "UNEXPECTED_EOF_WHILE_READING" in str(reason)
