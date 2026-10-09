"""Internal guest-cart credentials.

Cart views use these helpers. A cart UUID, Django session, or guest-session
UUID is not a credential. The raw token is accepted only from the
Authorization header by the HTTP layer.

Token format:
``secrets.token_urlsafe(32)`` produces 43 case-sensitive characters from
``A-Za-z0-9_-``. Presented tokens must already have that exact type, length
and syntax. They are not trimmed, lowercased or repaired.

Hashing:
SHA-256 of the token's exact UTF-8 bytes, stored as the lowercase 64-character
hexadecimal digest. The raw token is not stored or logged.

Lifetime:
30 days from one issuance timestamp, stored in ``expires_at``. A later read
does not move that timestamp. A session is active only while ``revoked_at``
is null and ``expires_at`` is strictly later than the current time.

Lock order, when ``for_update=True``:
lock ``GuestSession`` first, re-check expiry and revocation, then lock that
session's ``Cart``. Both locks belong to the caller's open transaction.
Do not take them through a nullable outer join.

Revocation does not undo an operation that already passed its access check.
Later operations must observe ``revoked_at``.

A guest session is not proof of a person's identity or email address.
Losing the raw token cannot be recovered here.
"""

import hashlib
import re
import secrets
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Cart, GuestSession


TOKEN_BYTES = 32
TOKEN_LENGTH = 43
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
GUEST_SESSION_LIFETIME = timedelta(days=30)
_UNAVAILABLE = "Cart access is not available."


class GuestAccessError(Exception):
    """Every failed lookup has this one result.

    Missing, malformed, unknown, expired, revoked, mismatched and legacy
    carts are not distinguished. The message contains no token, cart id or
    session id.
    """

    code = "UNAVAILABLE"

    def __init__(self):
        super().__init__(_UNAVAILABLE)


class IssuedGuestAccess:
    """Fresh cart plus the raw token returned to the internal caller.

    ``repr`` omits the raw token. Callers that need it use ``raw_token``.
    """

    __slots__ = ("cart", "_raw_token")

    def __init__(self, *, cart, raw_token):
        self.cart = cart
        self._raw_token = raw_token

    @property
    def raw_token(self):
        return self._raw_token

    def __repr__(self):
        return f"<IssuedGuestAccess cart_id={self.cart.id}>"


def issue_guest_cart():
    """Create one new guest session and its new cart.

    The caller cannot supply a cart, token, Django session or model fields.
    A failure inside the transaction leaves neither row.
    """
    raw_token = _new_raw_token()
    token_hash = _hash_valid_token(raw_token)
    issued_at = timezone.now()
    with transaction.atomic():
        guest_session = GuestSession.objects.create(
            token_hash=token_hash,
            created_at=issued_at,
            expires_at=issued_at + GUEST_SESSION_LIFETIME,
        )
        cart = Cart.objects.create(guest_session=guest_session)
    return IssuedGuestAccess(cart=cart, raw_token=raw_token)


def get_guest_session(*, raw_token, for_update=False):
    """Return the active session for this raw token.

    ``for_update=True`` locks that session until the caller's transaction
    ends. It raises ``RuntimeError`` when no transaction is open.
    """
    token_hash = _hash_presented_token(raw_token)
    if for_update:
        _require_open_transaction()
        queryset = GuestSession.objects.select_for_update()
    else:
        queryset = GuestSession.objects
    guest_session = queryset.filter(token_hash=token_hash).first()
    if guest_session is None or not _session_is_active(guest_session):
        raise GuestAccessError()
    return guest_session


def get_guest_cart(*, cart_id, raw_token, for_update=False):
    """Return the cart only when the token's active session owns that cart."""
    parsed_cart_id = _parse_cart_id(cart_id)
    guest_session = get_guest_session(raw_token=raw_token, for_update=for_update)
    cart = _session_cart(guest_session, for_update=for_update)
    if cart is None or cart.id != parsed_cart_id:
        raise GuestAccessError()
    return cart


def revoke_guest_session(*, raw_token):
    """Mark the token's session revoked, without deleting its cart.

    A second call for the same token is a no-op. A guest-session UUID is
    not accepted. The session row is locked for the duration of this
    transaction. Work that already passed an access check may still commit;
    this does not roll that work back.
    """
    token_hash = _hash_presented_token(raw_token)
    with transaction.atomic():
        _require_open_transaction()
        guest_session = (
            GuestSession.objects.select_for_update().filter(token_hash=token_hash).first()
        )
        if guest_session is None:
            raise GuestAccessError()
        if guest_session.revoked_at is None:
            guest_session.revoked_at = timezone.now()
            guest_session.save(update_fields=["revoked_at"])
    return guest_session


def replace_missing_guest_cart(*, raw_token):
    """Return this session's cart, creating an empty one only when it has none.

    The session row is locked and rechecked before the cart is read. An
    existing cart is returned unchanged, including its lines. The token,
    expiry and revocation state are not modified, and no caller-selected
    cart is attached.
    """
    with transaction.atomic():
        guest_session = get_guest_session(raw_token=raw_token, for_update=True)
        cart = _session_cart(guest_session, for_update=True)
        if cart is not None:
            return cart, False
        try:
            with transaction.atomic():
                cart = Cart.objects.create(guest_session=guest_session)
        except IntegrityError:
            cart = _session_cart(guest_session, for_update=True)
            if cart is None:
                raise
            return cart, False
        return cart, True


def _new_raw_token():
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    if TOKEN_PATTERN.fullmatch(raw_token) is None:
        raise RuntimeError("Generated guest token did not match the documented syntax.")
    return raw_token


def _hash_presented_token(raw_token):
    if not isinstance(raw_token, str) or TOKEN_PATTERN.fullmatch(raw_token) is None:
        raise GuestAccessError()
    return _hash_valid_token(raw_token)


def _hash_valid_token(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _parse_cart_id(cart_id):
    if not isinstance(cart_id, str):
        raise GuestAccessError()
    try:
        return Cart._meta.pk.to_python(cart_id)
    except (TypeError, ValueError, ValidationError):
        raise GuestAccessError()


def _session_is_active(guest_session):
    return (
        guest_session.revoked_at is None
        and guest_session.expires_at > timezone.now()
    )


def _session_cart(guest_session, *, for_update):
    queryset = Cart.objects.filter(guest_session_id=guest_session.pk)
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.first()


def _require_open_transaction():
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "GuestSession must be locked inside the caller's atomic transaction."
        )
