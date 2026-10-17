"""Public unpaid-order submission and confirmation.

The guest bearer is the credential. An order reference, cart UUID, email
address or customer payload is not. Repeated calls of the internal
``create_order_from_cart`` helper stay non-idempotent. This module is the
only place that folds a guest and an idempotency key into one order.
"""

import hashlib
import json
import re

from django.db import IntegrityError, transaction

from cart.http_access import resolve_guest_cart

from .creation import (
    _customer_details,
    _is_reference_integrity_error,
    create_order_from_cart,
)
from .exceptions import OrderCreationError
from .models import Order, OrderCreationAttempt


_KEY = re.compile(r"\A[A-Za-z0-9_-]{8,80}\Z")
ATTEMPT_CONSTRAINT = "order_creation_guest_key_uniq"


class IdempotencyConflict(OrderCreationError):
    def __init__(self):
        super().__init__(
            "IDEMPOTENCY_CONFLICT",
            "This submission does not match the original request.",
        )


def customer_request_hash(**fields):
    """Validate customer fields and return a stable digest plus the values."""
    customer = _customer_details(**fields)
    encoded = json.dumps(customer, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return digest, customer


def parse_idempotency_key(value):
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise OrderCreationError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "An idempotency key is required.",
        )
    return value


def submit_guest_order(*, raw_token, idempotency_key, customer_fields):
    """Create one unpaid order, or return the order already stored for this key."""
    digest, customer = customer_request_hash(**customer_fields)
    key = parse_idempotency_key(idempotency_key)
    try:
        with transaction.atomic():
            cart = resolve_guest_cart(
                raw_token=raw_token,
                asserted_cart_id=None,
                for_update=True,
            )
            session = cart.guest_session
            attempt = OrderCreationAttempt(
                guest_session=session,
                idempotency_key=key,
                request_hash=digest,
            )
            attempt.save()
            order = create_order_from_cart(
                cart_id=cart.id,
                guest_session=session,
                **customer,
            )
            attempt.order = order
            attempt.save(update_fields=["order"])
            return order, False
    except IntegrityError as exc:
        if _is_reference_integrity_error(exc) or not _is_attempt_conflict(exc):
            raise
    return _replay(raw_token, key, digest), True


def _replay(raw_token, key, digest):
    with transaction.atomic():
        cart = resolve_guest_cart(
            raw_token=raw_token,
            asserted_cart_id=None,
            for_update=True,
        )
        attempt = (
            OrderCreationAttempt.objects.select_for_update()
            .select_related("order")
            .get(guest_session=cart.guest_session, idempotency_key=key)
        )
        if attempt.request_hash != digest:
            raise IdempotencyConflict()
        if attempt.order_id is None:
            raise OrderCreationError(
                "ORDER_TEMPORARILY_UNAVAILABLE",
                "The order could not be confirmed.",
            )
        return attempt.order


def _is_attempt_conflict(exc):
    cause = getattr(exc, "__cause__", None)
    diag = getattr(cause, "diag", None) if cause is not None else None
    sqlstate = getattr(cause, "pgcode", None) if cause is not None else None
    if not sqlstate and cause is not None:
        sqlstate = getattr(cause, "sqlstate", None)
    if not sqlstate and diag is not None:
        sqlstate = getattr(diag, "sqlstate", None)
    constraint = getattr(diag, "constraint_name", None) if diag is not None else None
    if sqlstate == "23505" and constraint == ATTEMPT_CONSTRAINT:
        return True
    message = str(exc)
    marker = "UNIQUE constraint failed: "
    if marker not in message:
        return False
    detail = message.split(marker, 1)[1].strip()
    return detail == (
        "orders_ordercreationattempt.guest_session_id, "
        "orders_ordercreationattempt.idempotency_key"
    )


def order_for_guest(*, session, reference):
    """Return the guest's order, or None. Absence and mismatch look the same."""
    if not isinstance(reference, str) or not reference or len(reference) > 32:
        return None
    return (
        Order.objects.filter(reference=reference, guest_session=session)
        .prefetch_related("items__selected_options")
        .first()
    )


def public_order(order):
    """Customer-safe confirmation. No internal notes, ids, or secrets."""
    return {
        "reference": order.reference,
        "created_at": order.created_at.isoformat(),
        "customer_name": order.customer_name,
        "customer_email": order.customer_email,
        "customer_phone": order.customer_phone,
        "vehicle_make": order.vehicle_make,
        "vehicle_model": order.vehicle_model,
        "vehicle_year": order.vehicle_year,
        "vehicle_wheelbase": order.vehicle_wheelbase,
        "vehicle_registration": order.vehicle_registration,
        "customer_notes": order.customer_notes,
        "currency": order.currency,
        "full_total": format(order.full_total, "f"),
        "deposit_required": format(order.deposit_required, "f"),
        "balance_on_completion": format(order.balance_on_completion, "f"),
        "payment_status": order.payment_status,
        "fulfilment_method": order.fulfilment_method,
        "fitting_charge": format(order.fitting_charge, "f"),
        "tax_treatment": order.tax_treatment,
        "tax_amount": format(order.tax_amount, "f"),
        "payment_terms": order.payment_terms,
        "payment_terms_text": order.payment_terms_text,
        "items": [
            {
                "product_name": item.product_name,
                "product_slug": item.product_slug,
                "sku": item.sku,
                "quantity": item.quantity,
                "configured_unit_price": format(item.configured_unit_price, "f"),
                "line_total": format(item.line_total, "f"),
                "options": [
                    {
                        "group_name": option.group_name,
                        "option_name": option.option_name,
                        "price_adjustment": format(option.price_adjustment, "f"),
                    }
                    for option in item.selected_options.all()
                ],
            }
            for item in order.items.all()
        ],
    }
