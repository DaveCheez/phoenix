import logging

from django.db import IntegrityError, transaction

from .configuration import build_configuration_signature
from .exceptions import CartOptionError
from .models import CartItem, CartItemOption
from .option_selection import (
    configuration_signature_for_options,
    parse_requested_options,
    validate_product_options,
)


logger = logging.getLogger(__name__)
MAX_CART_QUANTITY = 999


def add_product_to_cart(*, cart, product, quantity, raw_options):
    requested = parse_requested_options(raw_options)
    resolved = validate_product_options(product, requested)
    signature = configuration_signature_for_options(resolved)
    return _add_configured_line(
        cart=cart,
        product=product,
        quantity=quantity,
        signature=signature,
        resolved=resolved,
    )


def _add_configured_line(*, cart, product, quantity, signature, resolved):
    return _increment_or_create(
        cart=cart,
        product=product,
        quantity=quantity,
        signature=signature,
        resolved=resolved,
        persist_options=True,
    )


def _increment_or_create(*, cart, product, quantity, signature, resolved, persist_options):
    item = (
        CartItem.objects.select_for_update()
        .filter(
            cart=cart,
            product=product,
            configuration_signature=signature,
        )
        .first()
    )
    created = item is None
    if item is None:
        try:
            with transaction.atomic():
                item = CartItem.objects.create(
                    cart=cart,
                    product=product,
                    quantity=quantity,
                    configuration_signature=signature,
                )
                if persist_options and resolved:
                    CartItemOption.objects.bulk_create(
                        [
                            CartItemOption(
                                cart_item=item,
                                group=option.group,
                                option=option,
                            )
                            for option in resolved
                        ]
                    )
        except IntegrityError:
            item = (
                CartItem.objects.select_for_update()
                .filter(
                    cart=cart,
                    product=product,
                    configuration_signature=signature,
                )
                .first()
            )
            if item is None:
                logger.error(
                    "Cart uniqueness conflict for product %s with no matching line",
                    product.id,
                )
                raise CartOptionError(
                    "CONFIGURATION_CONFLICT",
                    "This cart line could not be updated. Remove it and add the product again.",
                    http_status=409,
                )
            created = False

    if not created:
        _assert_persisted_options_match(item, signature)
        new_quantity = item.quantity + quantity
        if new_quantity > MAX_CART_QUANTITY:
            raise CartOptionError(
                "INVALID_QUANTITY",
                f"Quantity cannot be greater than {MAX_CART_QUANTITY}",
            )
        item.quantity = new_quantity
        item.save(update_fields=["quantity", "updated_at"])

    cart.save(update_fields=["updated_at"])
    return item, created


def _assert_persisted_options_match(item, signature):
    persisted_ids = list(item.selected_options.values_list("option_id", flat=True))
    try:
        persisted_signature = build_configuration_signature(persisted_ids)
    except ValueError:
        persisted_signature = None
    if persisted_signature != signature:
        logger.error(
            "Cart item %s signature %s does not match persisted options %s",
            item.id,
            signature,
            persisted_ids,
        )
        raise CartOptionError(
            "CONFIGURATION_CONFLICT",
            "This cart line could not be updated. Remove it and add the product again.",
            http_status=409,
        )
