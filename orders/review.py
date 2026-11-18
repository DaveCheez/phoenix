"""Opaque basket-review marker.

The marker is a SHA-256 digest of the priced basket and the commercial
terms that an order snapshot would copy. It is not a credential, not an
HMAC, and not a second pricing system. Image URLs and timestamps are left
out. Callers must send back the marker from the cart they reviewed.
"""

import hashlib
import hmac
import json
import re
from decimal import Decimal

from cart.exceptions import CartOptionError

from .exceptions import OrderCreationError
from .models import Order
from .money import calculate_deposit


PENNY = Decimal("0.01")
_TOKEN = re.compile(r"\A[0-9a-f]{64}\Z")
REVIEW_REQUIRED = (
    "CART_REVIEW_REQUIRED",
    "Review the basket before placing the order.",
)
CART_CHANGED_MESSAGE = "The basket has changed. Review it again before placing the order."


class CartChanged(OrderCreationError):
    def __init__(self):
        super().__init__("CART_CHANGED", CART_CHANGED_MESSAGE)


def require_review_token(value):
    """Accept only the exact digest syntax this module emits."""
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise OrderCreationError(*REVIEW_REQUIRED)
    return value


def review_token_for_lines(lines):
    """Digest the lines that this call will snapshot. Does not read again."""
    return _token(_document_from_lines(lines))


def review_token_for_canonical(lines, total):
    """Digest lines already priced for the cart response. Does not query."""
    return _token(_document(lines, total))


def canonical_line(
    *,
    product_id,
    product_name,
    product_slug,
    quantity,
    base,
    options_total,
    configured,
    line_total,
    options,
):
    return _item_row(
        product_id=product_id,
        product_name=product_name,
        product_slug=product_slug,
        quantity=quantity,
        base=base,
        options_total=options_total,
        configured=configured,
        line_total=line_total,
        options=options,
    )


def canonical_option(option, price, *, group=None):
    return _option_row(option, price, group=group)


def assert_current_review(lines, review_token):
    """Raise when the marker is not the digest of these already-priced lines."""
    actual = review_token_for_lines(lines)
    if not _same_token(actual, review_token):
        raise CartChanged()


def review_token_for_cart(cart):
    """Return the marker for this cart's current orderable snapshot.

    A cart that cannot be snapshotted still gets a marker for the stored
    rows, so the cart response can be returned. Order creation then reports
    the existing validation error, or ``CART_CHANGED`` once the snapshot
    succeeds with different content.
    """
    from .creation import _snapshot_lines

    try:
        lines = _snapshot_lines(cart)
    except (OrderCreationError, CartOptionError):
        return _token(_document_from_stored_cart(cart))
    return review_token_for_lines(lines)


def review_document_for_lines(lines):
    """Stable document used for the digest. Tests use this to see exclusions."""
    return _document_from_lines(lines)


def _document_from_lines(lines):
    rows = []
    total = Decimal("0.00")
    for line in lines:
        product = line["product"]
        options = [
            _option_row(selected["option"], selected["price_adjustment"])
            for selected in line["options"]
        ]
        total += line["line_total"]
        rows.append(
            _item_row(
                product_id=product.id,
                product_name=product.name,
                product_slug=product.slug,
                quantity=line["quantity"],
                base=line["base_unit_price"],
                options_total=line["options_total"],
                configured=line["configured_unit_price"],
                line_total=line["line_total"],
                options=options,
            )
        )
    return _document(rows, total)


def _document_from_stored_cart(cart):
    from cart.services import cart_items_queryset

    rows = []
    total = Decimal("0.00")
    for item in cart_items_queryset(cart):
        selections = sorted(
            item.selected_options.all(),
            key=lambda selection: (
                selection.group.display_order,
                selection.group_id,
                selection.option.display_order,
                selection.option_id,
            ),
        )
        base = item.product.price if item.product.price is not None else Decimal("0.00")
        options_total = sum(
            (
                selection.option.price_adjustment
                if selection.option.price_adjustment is not None
                else Decimal("0.00")
                for selection in selections
            ),
            Decimal("0.00"),
        )
        configured = base + options_total
        line_total = configured * item.quantity
        total += line_total
        rows.append(
            _item_row(
                product_id=item.product_id,
                product_name=item.product.name,
                product_slug=item.product.slug,
                quantity=item.quantity,
                base=base,
                options_total=options_total,
                configured=configured,
                line_total=line_total,
                options=[
                    _option_row(selection.option, selection.option.price_adjustment or Decimal("0.00"))
                    for selection in selections
                ],
            )
        )
    return _document(rows, total)


def _document(rows, total):
    if not isinstance(total, Decimal):
        total = Decimal(total)
    total = total.quantize(PENNY)
    split = calculate_deposit(total)
    return {
        "items": rows,
        "terms": {
            "balance_on_completion": _money(split.balance),
            "currency": Order.CURRENCY_GBP,
            "deposit_required": _money(split.deposit),
            "fitting_charge": _money(Decimal("0.00")),
            "fulfilment_method": Order.FULFILMENT_WORKSHOP_FITTING,
            "full_total": _money(total),
            "payment_terms": Order.PAYMENT_TERMS_THIRD_DEPOSIT,
            "payment_terms_text": Order.PAYMENT_TERMS_TEXT,
            "tax_amount": _money(Decimal("0.00")),
            "tax_treatment": Order.TAX_NOT_VAT_REGISTERED,
        },
    }


def _item_row(
    *,
    product_id,
    product_name,
    product_slug,
    quantity,
    base,
    options_total,
    configured,
    line_total,
    options,
):
    return {
        "base_unit_price": _money(base),
        "configured_unit_price": _money(configured),
        "line_total": _money(line_total),
        "options": options,
        "options_total": _money(options_total),
        "product_id": product_id,
        "product_name": product_name,
        "product_slug": product_slug,
        "quantity": quantity,
    }


def _option_row(option, price, group=None):
    chosen = option.group if group is None else group
    return {
        "group_id": option.group_id,
        "group_name": chosen.name,
        "option_id": option.id,
        "option_name": option.name,
        "option_sku": option.sku or "",
        "price_adjustment": _money(price),
    }


def _money(value):
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise OrderCreationError("INVALID_TOTAL", "The order total could not be calculated.")
    return format(value.quantize(PENNY), ".2f")


def _token(document):
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _same_token(actual, presented):
    if not isinstance(actual, str) or not isinstance(presented, str):
        return False
    if len(actual) != len(presented):
        return False
    return hmac.compare_digest(actual, presented)
