"""Create one unpaid order snapshot from the current rows in a cart.

This is an internal service. Knowing a cart UUID does not prove that the
caller owns the cart. Repeated calls are not idempotent: ``source_cart`` is
not unique, and a second successful call creates another order. Customer
authorisation and public-request idempotency are later work.

Locking limits:
- ``select_for_update()`` on the cart coordinates only writers that lock the
  same cart row.
- It does not freeze product or option rows.
- The product query and the option query are separate reads, not one
  database-wide snapshot.
- SQLite tests do not prove PostgreSQL row-lock behaviour.

``cart_items_queryset()`` prefetches catalogue rows for ordering and for
``configuration_is_valid``. Snapshot names and prices are taken from a fresh
``Product`` read and from ``validate_product_options()``, which queries
options again. Those resolved objects are the only catalogue values copied.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import IntegrityError, transaction

from cart.configuration import build_configuration_signature
from cart.exceptions import CartOptionError
from cart.models import Cart, CartItemOption
from cart.option_selection import validate_product_options
from cart.services import cart_items_queryset, configuration_is_valid
from store.models import Product

from .exceptions import OrderCreationError
from .models import MAX_ORDER_QUANTITY, Order, OrderItem, OrderItemOption, generate_order_reference
from .money import calculate_deposit


REFERENCE_ATTEMPTS = 5
MAX_MONEY = Decimal("9999999999.99")
PENNY = Decimal("0.01")

_EMAIL = EmailValidator()


def create_order_from_cart(
    *,
    cart_id,
    customer_name,
    customer_email,
    customer_phone,
    vehicle_make,
    vehicle_model,
    vehicle_year,
    vehicle_wheelbase,
    vehicle_registration="",
    customer_notes="",
) -> Order:
    """Build a finalised unpaid snapshot from the cart's current database rows.

    Prices, totals, deposit, reference, currency, payment state and
    fulfilment terms are calculated or set here. Callers cannot supply them.
    """
    customer = _customer_details(
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        vehicle_make=vehicle_make,
        vehicle_model=vehicle_model,
        vehicle_year=vehicle_year,
        vehicle_wheelbase=vehicle_wheelbase,
        vehicle_registration=vehicle_registration,
        customer_notes=customer_notes,
    )

    with transaction.atomic():
        cart = _lock_cart(cart_id)
        lines = _snapshot_lines(cart)
        full_total = sum((line["line_total"] for line in lines), Decimal("0.00"))
        full_total = _exact_money(full_total)
        split = calculate_deposit(full_total)
        order = _insert_order(cart=cart, customer=customer, full_total=full_total, split=split)
        _insert_lines(order, lines)
        return order._mark_finalised()


def _customer_details(
    *,
    customer_name,
    customer_email,
    customer_phone,
    vehicle_make,
    vehicle_model,
    vehicle_year,
    vehicle_wheelbase,
    vehicle_registration,
    customer_notes,
):
    return {
        "customer_name": _text(
            customer_name,
            code="INVALID_CUSTOMER",
            max_length=200,
            required=True,
        ),
        "customer_email": _email(customer_email),
        "customer_phone": _text(
            customer_phone,
            code="INVALID_CUSTOMER",
            max_length=40,
            required=True,
        ),
        "vehicle_make": _text(
            vehicle_make,
            code="INVALID_VEHICLE",
            max_length=100,
            required=True,
        ),
        "vehicle_model": _text(
            vehicle_model,
            code="INVALID_VEHICLE",
            max_length=100,
            required=True,
        ),
        "vehicle_year": _year(vehicle_year),
        "vehicle_wheelbase": _text(
            vehicle_wheelbase,
            code="INVALID_VEHICLE",
            max_length=50,
            required=True,
        ),
        "vehicle_registration": _text(
            vehicle_registration,
            code="INVALID_VEHICLE",
            max_length=20,
            required=False,
        ),
        "customer_notes": _text(
            customer_notes,
            code="INVALID_CUSTOMER",
            max_length=None,
            required=False,
        ),
    }


def _text(value, *, code, max_length, required):
    if not isinstance(value, str):
        raise OrderCreationError(code, _safe_message(code))
    cleaned = value.strip()
    if required and not cleaned:
        raise OrderCreationError(code, _safe_message(code))
    if max_length is not None and len(cleaned) > max_length:
        raise OrderCreationError(code, _safe_message(code))
    return cleaned


def _email(value):
    cleaned = _text(value, code="INVALID_CUSTOMER", max_length=254, required=True)
    try:
        _EMAIL(cleaned)
    except ValidationError:
        raise OrderCreationError("INVALID_CUSTOMER", _safe_message("INVALID_CUSTOMER"))
    return cleaned


def _year(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise OrderCreationError("INVALID_VEHICLE", _safe_message("INVALID_VEHICLE"))
    if value < 1900 or value > 2100:
        raise OrderCreationError("INVALID_VEHICLE", _safe_message("INVALID_VEHICLE"))
    return value


def _safe_message(code):
    if code == "INVALID_VEHICLE":
        return "Vehicle details could not be read."
    return "Customer details could not be read."


def _lock_cart(cart_id):
    try:
        return Cart.objects.select_for_update().get(pk=cart_id)
    except (Cart.DoesNotExist, ValidationError, ValueError, TypeError):
        raise OrderCreationError("CART_NOT_FOUND", "Cart not found.")


def _snapshot_lines(cart):
    """Return in-memory line snapshots in cart line order.

    ``cart_items_queryset`` fixes the line order and supplies the cart rows
    passed to ``configuration_is_valid``. Prices and labels come from
    ``product`` and ``resolved`` below, both loaded for this call.
    """
    items = list(cart_items_queryset(cart))
    if not items:
        raise OrderCreationError("EMPTY_CART", "The cart does not contain any items.")

    lines = []
    for position, item in enumerate(items, start=1):
        if (
            isinstance(item.quantity, bool)
            or not isinstance(item.quantity, int)
            or item.quantity < 1
            or item.quantity > MAX_ORDER_QUANTITY
        ):
            raise OrderCreationError(
                "INVALID_QUANTITY",
                "Cart quantities could not be used.",
            )

        try:
            product = Product.objects.get(pk=item.product_id)
        except Product.DoesNotExist:
            raise CartOptionError(
                "INVALID_OPTION",
                "One or more selected options are unavailable.",
            )

        selections = list(
            CartItemOption.objects.select_related("group", "option")
            .filter(cart_item_id=item.id)
            .order_by("id")
        )
        option_ids = [selection.option_id for selection in selections]
        try:
            signature = build_configuration_signature(option_ids)
        except ValueError:
            raise CartOptionError(
                "CONFIGURATION_CONFLICT",
                "This cart line could not be used to create an order.",
            )
        if signature != item.configuration_signature:
            raise CartOptionError(
                "CONFIGURATION_CONFLICT",
                "This cart line could not be used to create an order.",
            )

        resolved = validate_product_options(product, option_ids)
        resolved_by_id = {option.id: option for option in resolved}
        for selection in selections:
            resolved_option = resolved_by_id.get(selection.option_id)
            if resolved_option is None or selection.group_id != resolved_option.group_id:
                raise CartOptionError(
                    "INVALID_OPTION",
                    "One or more selected options are unavailable.",
                )

        if not configuration_is_valid(item, selections):
            raise CartOptionError(
                "CONFIGURATION_CONFLICT",
                "This cart line could not be used to create an order.",
            )

        base_unit_price = _exact_money(product.price)
        options_total = sum(
            (_exact_money(option.price_adjustment) for option in resolved),
            Decimal("0.00"),
        )
        options_total = _exact_money(options_total)
        configured_unit_price = _exact_money(base_unit_price + options_total)
        line_total = _exact_money(configured_unit_price * item.quantity)
        lines.append(
            {
                "position": position,
                "product": product,
                "signature": signature,
                "quantity": item.quantity,
                "base_unit_price": base_unit_price,
                "options_total": options_total,
                "configured_unit_price": configured_unit_price,
                "line_total": line_total,
                "options": [
                    {
                        "position": option_position,
                        "option": option,
                        "price_adjustment": _exact_money(option.price_adjustment),
                    }
                    for option_position, option in enumerate(resolved, start=1)
                ],
            }
        )
    return lines


def _exact_money(value):
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise OrderCreationError("INVALID_TOTAL", "The order total could not be calculated.")
    if value < Decimal("0.00") or value > MAX_MONEY or value != value.quantize(PENNY):
        raise OrderCreationError("INVALID_TOTAL", "The order total could not be calculated.")
    return value


def _insert_order(*, cart, customer, full_total, split):
    """Insert one order, retrying only a reference-uniqueness collision."""
    for _attempt in range(REFERENCE_ATTEMPTS):
        reference = generate_order_reference()
        try:
            with transaction.atomic():
                order = Order(
                    reference=reference,
                    is_finalised=False,
                    currency=Order.CURRENCY_GBP,
                    fulfilment_method=Order.FULFILMENT_WORKSHOP_FITTING,
                    fitting_charge=Decimal("0.00"),
                    tax_treatment=Order.TAX_NOT_VAT_REGISTERED,
                    tax_amount=Decimal("0.00"),
                    payment_terms=Order.PAYMENT_TERMS_THIRD_DEPOSIT,
                    payment_terms_text=Order.PAYMENT_TERMS_TEXT,
                    amount_paid=Decimal("0.00"),
                    payment_status=Order.PAYMENT_PENDING_DEPOSIT,
                    job_status=Order.JOB_NEW,
                    full_total=full_total,
                    deposit_required=split.deposit,
                    balance_on_completion=split.balance,
                    source_cart=cart,
                    **customer,
                )
                order.save()
                return order
        except ValidationError as exc:
            if not _is_reference_only_validation_error(exc):
                raise
        except IntegrityError as exc:
            if not _is_reference_integrity_error(exc):
                raise
    raise OrderCreationError(
        "REFERENCE_UNAVAILABLE",
        "An order reference could not be reserved.",
    )


def _insert_lines(order, lines):
    for line in lines:
        item = OrderItem.objects.create(
            order=order,
            position=line["position"],
            original_product_id=line["product"].id,
            product=line["product"],
            product_name=line["product"].name,
            product_slug=line["product"].slug,
            sku="",
            configuration_signature=line["signature"],
            quantity=line["quantity"],
            base_unit_price=line["base_unit_price"],
            options_total=line["options_total"],
            configured_unit_price=line["configured_unit_price"],
            line_total=line["line_total"],
        )
        for selected in line["options"]:
            option = selected["option"]
            OrderItemOption.objects.create(
                order_item=item,
                position=selected["position"],
                original_group_id=option.group_id,
                original_option_id=option.id,
                group=option.group,
                option=option,
                group_name=option.group.name,
                option_name=option.name,
                option_sku=option.sku or "",
                price_adjustment=selected["price_adjustment"],
            )


# 0001 declares reference unique=True. Django 4.1 emits that as an unnamed
# UNIQUE in CREATE TABLE, so PostgreSQL names it orders_order_reference_key.
# This is the name implied by that schema. It has not been read from
# pg_constraint.
REFERENCE_CONSTRAINT_NAME = "orders_order_reference_key"
REFERENCE_TABLE_NAME = "orders_order"
POSTGRES_UNIQUE_VIOLATION = "23505"


def _is_reference_only_validation_error(exc):
    error_dict = getattr(exc, "error_dict", None)
    if not isinstance(error_dict, dict) or set(error_dict) != {"reference"}:
        return False
    errors = error_dict["reference"]
    return bool(errors) and all(getattr(error, "code", None) == "unique" for error in errors)


def _postgres_reference_metadata(cause):
    """Return driver metadata, or None when this is not a structured driver error.

    psycopg2 2.9 exposes SQLSTATE as ``Error.pgcode`` and again as
    ``diag.sqlstate``. ``diag.constraint_name`` and ``diag.table_name`` are
    the constraint fields. A missing attribute stays None. When none of
    those fields exist, the caller uses the SQLite message instead.
    """
    if cause is None:
        return None
    diag = getattr(cause, "diag", None)
    sqlstate = getattr(cause, "pgcode", None)
    if not sqlstate:
        sqlstate = getattr(cause, "sqlstate", None)
    if not sqlstate and diag is not None:
        sqlstate = getattr(diag, "sqlstate", None)
    constraint = getattr(diag, "constraint_name", None) if diag is not None else None
    table = getattr(diag, "table_name", None) if diag is not None else None
    if not sqlstate and not constraint and not table:
        return None
    return sqlstate or None, constraint or None, table or None


def _is_reference_integrity_error(exc):
    """True only for a duplicate Order.reference.

    PostgreSQL must supply SQLSTATE 23505 and constraint
    ``orders_order_reference_key``. A table name, when the driver supplies
    one, must be ``orders_order``. SQLSTATE alone is not enough, and a
    PostgreSQL message is not parsed when that metadata does not match.

    SQLite must report exactly ``UNIQUE constraint failed: orders_order.reference``.
    """
    metadata = _postgres_reference_metadata(getattr(exc, "__cause__", None))
    if metadata is not None:
        sqlstate, constraint, table = metadata
        if sqlstate != POSTGRES_UNIQUE_VIOLATION:
            return False
        if constraint != REFERENCE_CONSTRAINT_NAME:
            return False
        if table not in (None, REFERENCE_TABLE_NAME):
            return False
        return True

    message = str(exc)
    marker = "UNIQUE constraint failed: "
    if marker not in message:
        return False
    return message.split(marker, 1)[1].strip() == "orders_order.reference"
