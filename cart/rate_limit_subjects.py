"""HMAC subject keys for cart rate-limit counters.

A digest is a pseudonym. Anyone who has the HMAC key can recompute it from
the same address or guest-session id. It is not anonymous.

Key derivation is not authentication. Callers must already have accepted the
application credential and shopper address, or resolved the guest session,
before asking for a key. A browser-supplied digest is not authority.

Changing ``CART_RATE_LIMIT_HMAC_KEY`` changes every digest. Existing counter
rows would no longer match, which resets effective budgets. Rotation needs a
later operational procedure. This module does not rotate keys.

IPv6 addresses are grouped at /64 before hashing. That is a counter grouping
policy. It does not mean a /64 is one person or one household. Address scopes
use that grouping. Session scopes do not.
"""

import hashlib
import hmac
import ipaddress
import re
import uuid

from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from .application_access import ApplicationAccessRejected, canonical_shopper_address
from .models import RATE_LIMIT_SCOPES


_KEY_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
_PLACEHOLDER_MARKERS = (
    "replace",
    "placeholder",
    "changeme",
    "example",
    "dummy",
    "password",
    "todo",
)
_DOMAIN = b"phoenix-vanz.cart-rate-limit.v1"
_ADDRESS_SCOPES = frozenset({"issuance", "failed_access", "csrf", "reset"})
_SESSION_SCOPES = frozenset({"authenticated_cart"})


class SubjectKeyError(Exception):
    """The subject key could not be derived. The message names no subject."""


@sensitive_variables()
def subject_key_for_address(*, scope, address):
    """Digest one canonical address for an address scope.

    Address scopes are issuance, failed_access, csrf and reset. A guest
    session is not an address subject.
    """
    if scope not in _ADDRESS_SCOPES:
        raise SubjectKeyError("Scope does not accept an address subject.")
    try:
        canonical = canonical_shopper_address(address)
    except ApplicationAccessRejected:
        raise SubjectKeyError("Shopper address is not usable for a counter key.") from None
    grouped = _group_address(canonical)
    return _digest(scope=scope, kind="address", subject=grouped)


@sensitive_variables()
def subject_key_for_guest_session(*, scope, session_id):
    """Digest one guest-session UUID for the authenticated-cart scope.

    The UUID is the server-side primary key. This does not read the row,
    the raw bearer, or the token hash.
    """
    if scope not in _SESSION_SCOPES:
        raise SubjectKeyError("Scope does not accept a guest-session subject.")
    try:
        if isinstance(session_id, uuid.UUID):
            canonical = str(session_id)
        elif isinstance(session_id, str):
            canonical = str(uuid.UUID(session_id))
        else:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise SubjectKeyError("Guest session id is not usable for a counter key.") from None
    return _digest(scope=scope, kind="guest_session", subject=canonical)


@sensitive_variables()
def _group_address(canonical):
    parsed = ipaddress.ip_address(canonical)
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(parsed)
    network = ipaddress.IPv6Network((parsed, 64), strict=False)
    return network.network_address.compressed


@sensitive_variables()
def _digest(*, scope, kind, subject):
    if scope not in RATE_LIMIT_SCOPES or kind not in {"address", "guest_session"}:
        raise SubjectKeyError("Subject key domain is not valid.")
    message = b"\0".join(
        (
            _DOMAIN,
            scope.encode("ascii"),
            kind.encode("ascii"),
            subject.encode("ascii"),
        )
    )
    return hmac.new(_hmac_key_bytes(), message, hashlib.sha256).hexdigest()


@sensitive_variables()
def _hmac_key_bytes():
    value = getattr(settings, "CART_RATE_LIMIT_HMAC_KEY", "")
    if (
        not isinstance(value, str)
        or _KEY_PATTERN.fullmatch(value) is None
        or _is_placeholder(value)
    ):
        raise SubjectKeyError("Cart rate limit HMAC key is not configured.")
    return bytes.fromhex(value)


def _is_placeholder(value):
    folded = value.casefold()
    if any(marker in folded for marker in _PLACEHOLDER_MARKERS):
        return True
    return value == ("0" * 64) or value == ("f" * 64)
