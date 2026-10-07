"""Optional HTTP accounting in front of cart data routes.

Enforcement is off unless ``CART_RATE_LIMIT_ENABLED`` is the boolean True.
Policies and the HMAC key are server configuration. A request cannot supply
them.

The application gate still runs first. This module is only used after that
gate has accepted one shopper-address assertion. A request that never obtains
that assertion is not placed in an address budget. Edge protection is still
required for those unauthenticated floods.

Accounting commits in its own transaction before any cart or guest-session
write. ``GET`` may insert counter rows when enforcement is on. It still does
not change a cart, guest session, selection, or expiry.

A later business failure does not refund the committed attempt. This module
does not retry and does not mint a replacement guest identity.
"""

import json
import logging
from dataclasses import dataclass

from django.conf import settings
from django.views.decorators.debug import sensitive_variables
from rest_framework import status
from rest_framework.response import Response

from .guest_access import GuestAccessError, get_guest_session
from .models import RATE_LIMIT_SCOPES
from .rate_limit import (
    RateLimitNestedTransactionError,
    RateLimitStoreError,
    RateLimitValidationError,
    RateLimitWindow,
    consume_windows,
)
from .rate_limit_subjects import (
    SubjectKeyError,
    subject_key_for_address,
    subject_key_for_guest_session,
)


logger = logging.getLogger(__name__)

_SCOPES = ("issuance", "failed_access", "authenticated_cart")
_UNAVAILABLE_BODY = {
    "success": False,
    "code": "CART_TEMPORARILY_UNAVAILABLE",
    "error": "Cart access could not be completed.",
}
_LIMITED_BODY = {
    "success": False,
    "code": "CART_RATE_LIMITED",
    "error": "Cart access is temporarily limited.",
}


class RateLimitConfigurationError(Exception):
    """Enabled enforcement is not safely configured. No subject is included."""


@dataclass(frozen=True)
class FailedAccessStoreOutage:
    """The guest denial stands. Accounting could not record it."""


FAILED_ACCESS_STORE_OUTAGE = FailedAccessStoreOutage()


def enforcement_enabled():
    value = getattr(settings, "CART_RATE_LIMIT_ENABLED", False)
    if not isinstance(value, bool):
        raise RateLimitConfigurationError()
    return value


@sensitive_variables()
def account_issuance(address):
    """Count one explicit anonymous start. Returns a response or None."""
    return _account(scope="issuance", address=address, session_id=None)


@sensitive_variables()
def account_failed_access(address):
    """Count one rejected guest credential.

    A store outage returns ``FAILED_ACCESS_STORE_OUTAGE`` so the caller can
    keep the normal guest 401. Configuration errors return 503.
    """
    return _account(scope="failed_access", address=address, session_id=None)


@sensitive_variables()
def account_authenticated(address, session_id):
    """Count one active guest session. A store outage returns None to proceed."""
    return _account(
        scope="authenticated_cart",
        address=address,
        session_id=session_id,
    )


@sensitive_variables()
def resolve_active_session(raw_token):
    """Read the active session without taking a business row lock."""
    return get_guest_session(raw_token=raw_token, for_update=False)


@sensitive_variables()
def _account(*, scope, address, session_id):
    try:
        if not enforcement_enabled():
            return None
        windows = _policy_windows(scope)
        subject_key = _subject_key(scope=scope, address=address, session_id=session_id)
        decision = consume_windows(scope=scope, subject_key=subject_key, windows=windows)
    except RateLimitStoreError:
        logger.warning("cart_rate_limit_store_unavailable %s", scope)
        if scope == "issuance":
            return _unavailable()
        if scope == "failed_access":
            return FAILED_ACCESS_STORE_OUTAGE
        logger.warning("cart_rate_limit_authenticated_unavailable")
        return None
    except (
        RateLimitConfigurationError,
        RateLimitValidationError,
        RateLimitNestedTransactionError,
        SubjectKeyError,
    ):
        logger.warning("cart_rate_limit_configuration_rejected")
        return _unavailable()
    if decision.allowed:
        return None
    return _limited(decision.retry_after_seconds)


@sensitive_variables()
def _subject_key(*, scope, address, session_id):
    if scope == "authenticated_cart":
        return subject_key_for_guest_session(scope=scope, session_id=session_id)
    return subject_key_for_address(scope=scope, address=address)


def _policy_windows(scope):
    policies = _load_policies()
    if set(policies) != set(_SCOPES) or scope not in policies:
        raise RateLimitConfigurationError()
    entries = policies[scope]
    if isinstance(entries, (str, bytes)) or not isinstance(entries, (list, tuple)):
        raise RateLimitConfigurationError()
    if len(entries) < 1:
        raise RateLimitConfigurationError()
    windows = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"window_seconds", "limit"}:
            raise RateLimitConfigurationError()
        windows.append(
            RateLimitWindow(
                window_seconds=entry["window_seconds"],
                limit=entry["limit"],
            )
        )
    if scope not in RATE_LIMIT_SCOPES:
        raise RateLimitConfigurationError()
    return tuple(windows)


def _load_policies():
    raw = getattr(settings, "CART_RATE_LIMIT_POLICIES", "")
    if isinstance(raw, str):
        if raw.strip() == "":
            raise RateLimitConfigurationError()
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise RateLimitConfigurationError() from None
    if not isinstance(raw, dict):
        raise RateLimitConfigurationError()
    return raw


def _limited(retry_after):
    response = Response(_LIMITED_BODY, status=status.HTTP_429_TOO_MANY_REQUESTS)
    response["Cache-Control"] = "no-store"
    response["Retry-After"] = str(int(retry_after))
    return response


def _unavailable():
    response = Response(_UNAVAILABLE_BODY, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    response["Cache-Control"] = "no-store"
    return response
