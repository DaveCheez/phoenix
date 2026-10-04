"""Bearer parsing and cart selection for the cart HTTP routes.

Django's request interface exposes one ``HTTP_AUTHORIZATION`` string. PEP 3333
does not give the application a list of repeated header fields. A server that
joins duplicate Authorization values with a comma is rejected here. A server
that keeps only one of those values does not show the discarded value to
Django, so this process cannot claim to have seen every raw duplicate.

The token is taken only from that header. Body fields, query fields, cookies,
``cart_id``, ``session_key``, a guest-session UUID and any shared application
secret are not credentials.
"""

import re
from collections.abc import Mapping

from django.http import QueryDict
from django.views.decorators.debug import sensitive_variables

from .guest_access import GuestAccessError, get_guest_cart, get_guest_session
from .models import Cart


_BEARER = re.compile(r"(?i:Bearer) ([A-Za-z0-9_-]{43})\Z")
_AMBIGUOUS = (",", "\t", '"', "'", "\r", "\n")


class GuestCartMissing(Exception):
    """The presented bearer is active and its session has no cart."""


class BearerCredentials:
    """An absent header, or one exact token copied from that header."""

    __slots__ = ("absent", "token")

    def __init__(self, *, absent, token):
        self.absent = absent
        self.token = token


@sensitive_variables()
def read_bearer(request):
    """Return the header state without repairing the token.

    An absent header is different from a present empty or malformed header.
    A present bad header raises ``GuestAccessError`` and must not be treated
    as permission to start a new guest cart.
    """
    if "HTTP_AUTHORIZATION" not in request.META:
        return BearerCredentials(absent=True, token=None)
    raw = request.META.get("HTTP_AUTHORIZATION")
    header_value = request.headers.get("Authorization")
    if raw != header_value or not isinstance(raw, str) or raw == "":
        raise GuestAccessError()
    if any(character in raw for character in _AMBIGUOUS):
        raise GuestAccessError()
    matched = _BEARER.fullmatch(raw)
    if matched is None:
        raise GuestAccessError()
    return BearerCredentials(absent=False, token=matched.group(1))


def supplied_cart_ids(request):
    """Return every body and query cart_id without choosing between them."""
    values = []
    params = getattr(request, "query_params", None)
    if params is not None and "cart_id" in params:
        values.extend(params.getlist("cart_id"))
    data = getattr(request, "data", None)
    if isinstance(data, QueryDict) and "cart_id" in data:
        values.extend(data.getlist("cart_id"))
    elif isinstance(data, Mapping) and "cart_id" in data:
        values.append(data.get("cart_id"))
    return values


def consistent_cart_id(request):
    """Return one supplied cart id, or None when the caller supplied none.

    Differing body and query values, repeated values, and non-strings raise
    ``GuestAccessError`` instead of preferring one of them.
    """
    values = supplied_cart_ids(request)
    if not values:
        return None
    first = values[0]
    if not isinstance(first, str) or any(value != first for value in values):
        raise GuestAccessError()
    return first


def single_action(request):
    data = getattr(request, "data", None)
    if isinstance(data, QueryDict):
        values = data.getlist("action") if "action" in data else []
    elif isinstance(data, Mapping) and "action" in data:
        values = [data.get("action")]
    else:
        values = []
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]


def is_exact_start_body(request):
    data = getattr(request, "data", None)
    if isinstance(data, QueryDict):
        return list(data.keys()) == ["action"] and data.getlist("action") == ["start"]
    if isinstance(data, Mapping):
        return set(data.keys()) == {"action"} and data.get("action") == "start"
    return False


@sensitive_variables()
def resolve_guest_cart(*, raw_token, asserted_cart_id, for_update):
    """Select the session's cart, then optionally assert a supplied id.

    The lookup key is the bearer session. A supplied id is compared with that
    cart and is never used to load a different cart.
    """
    session = get_guest_session(raw_token=raw_token, for_update=for_update)
    owned_id = (
        Cart.objects.filter(guest_session_id=session.pk)
        .values_list("id", flat=True)
        .first()
    )
    if owned_id is None:
        if asserted_cart_id is not None:
            raise GuestAccessError()
        raise GuestCartMissing()
    cart_id = asserted_cart_id if asserted_cart_id is not None else str(owned_id)
    return get_guest_cart(
        cart_id=cart_id,
        raw_token=raw_token,
        for_update=for_update,
    )
