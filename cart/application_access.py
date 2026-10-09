"""Application authentication for cart data routes.

The application credential identifies the calling server. It is not a guest
bearer and does not prove which shopper owns a cart.

Django accepts ``X-Phoenix-Shopper-Address`` only after that credential
matches. It cannot check that the authorised server put the shopper's real
address there. ``REMOTE_ADDR``, ``do-connecting-ip``, ``X-Forwarded-For``
and body or query fields are not substitutes.

Django's request interface exposes one string per header. PEP 3333 does not
give the application a list of repeated header fields. A comma-joined value
is rejected. A server that keeps only one of two raw duplicate headers does
not show the discarded value to Django.
"""

import hmac
import ipaddress
import re

from django.conf import settings
from django.views.decorators.debug import sensitive_variables


_CREDENTIAL_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
_MAX_ADDRESS_LENGTH = 45
_HEADER_AMBIGUOUS = (",", "\t", '"', "'", "\r", "\n")
_ADDRESS_MARKERS = (",", '"', "'", "[", "]", "/", "%", "?", "#", "@")
_PLACEHOLDER_MARKERS = (
    "replace",
    "placeholder",
    "changeme",
    "example",
    "dummy",
    "password",
    "todo",
)


class ApplicationAccessRejected(Exception):
    """Cart data cannot proceed. The message discloses nothing."""


@sensitive_variables()
def authenticate_cart_application(request):
    """Require the application credential, then one canonical shopper address.

    Returns the canonical address. Callers must not log it.
    """
    presented = _single_header(
        request,
        "HTTP_X_PHOENIX_APP_CREDENTIAL",
        "X-Phoenix-App-Credential",
    )
    expected = _configured_credential()
    if _CREDENTIAL_PATTERN.fullmatch(presented) is None:
        raise ApplicationAccessRejected()
    if not hmac.compare_digest(presented, expected):
        raise ApplicationAccessRejected()
    raw_address = _single_header(
        request,
        "HTTP_X_PHOENIX_SHOPPER_ADDRESS",
        "X-Phoenix-Shopper-Address",
    )
    return canonical_shopper_address(raw_address)


@sensitive_variables()
def _configured_credential():
    value = getattr(settings, "CART_APP_CREDENTIAL", "")
    if (
        not isinstance(value, str)
        or _CREDENTIAL_PATTERN.fullmatch(value) is None
        or _is_placeholder(value)
    ):
        raise ApplicationAccessRejected()
    return value


def _is_placeholder(value):
    folded = value.casefold()
    if any(marker in folded for marker in _PLACEHOLDER_MARKERS):
        return True
    return value == ("0" * 64) or value == ("f" * 64)


@sensitive_variables()
def _single_header(request, meta_name, header_name):
    if meta_name not in request.META:
        raise ApplicationAccessRejected()
    raw = request.META.get(meta_name)
    header_value = request.headers.get(header_name)
    if raw != header_value or not isinstance(raw, str) or raw == "":
        raise ApplicationAccessRejected()
    if any(character in raw for character in _HEADER_AMBIGUOUS):
        raise ApplicationAccessRejected()
    return raw


@sensitive_variables()
def canonical_shopper_address(value):
    """Return one canonical IPv4 or IPv6 address, or reject the value."""
    if not isinstance(value, str) or not value.isascii():
        raise ApplicationAccessRejected()
    if len(value) == 0 or len(value) > _MAX_ADDRESS_LENGTH:
        raise ApplicationAccessRejected()
    if any(character.isspace() for character in value):
        raise ApplicationAccessRejected()
    if any(character in value for character in _ADDRESS_MARKERS):
        raise ApplicationAccessRejected()
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        raise ApplicationAccessRejected() from None
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    return parsed.compressed
