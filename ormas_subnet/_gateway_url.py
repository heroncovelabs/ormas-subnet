"""Gateway URL validation shared by bearer-bearing HTTP clients."""
from __future__ import annotations

from urllib.parse import urlsplit


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def require_secure_gateway_url(value: str, *, allow_local_transport: bool = False) -> str:
    """Return a normalized gateway URL or refuse a credential-exposing origin.

    HTTPS is required for remote hosts.  Plain HTTP is limited to the exact
    loopback names used by local development.  ``local://<name>`` is an
    in-process sentinel accepted only when the caller supplied an injected
    transport, so it can never create a network client.
    """
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("gateway must be an HTTP or HTTPS URL without credentials")
    try:
        url = urlsplit(value)
        # Accessing ``port`` forces urllib to reject malformed and out-of-range
        # ports before an HTTP library gets a chance to reinterpret them.
        url.port
    except ValueError:
        raise ValueError(
            "gateway must be an HTTP or HTTPS URL without credentials"
        ) from None
    if (
        allow_local_transport
        and url.scheme == "local"
        and bool(url.hostname)
        and url.username is None
        and url.password is None
        and not url.query
        and not url.fragment
        and url.path in ("", "/")
    ):
        return value.rstrip("/")
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or bool(url.query)
        or bool(url.fragment)
    ):
        raise ValueError("gateway must be an HTTP or HTTPS URL without credentials")
    if url.scheme == "http" and url.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("gateway must use HTTPS; HTTP is allowed only for loopback hosts")
    return value.rstrip("/")
