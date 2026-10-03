from decimal import Decimal

from django.conf import settings
from django.db.models import Prefetch

from .configuration import build_configuration_signature
from .models import Cart, CartItemOption
from .option_selection import active_groups_prefetch


def money(value: Decimal) -> str:
    """Return a stable two-decimal currency string for API responses."""
    return format(value.quantize(Decimal("0.01")), ".2f")


def cart_items_queryset(cart: Cart):
    """Load cart lines with catalogue data needed for pricing and validity."""
    return (
        cart.items.select_related("product")
        .prefetch_related(
            "product__productimage_set",
            Prefetch(
                "selected_options",
                queryset=CartItemOption.objects.select_related("group", "option").order_by(
                    "group__display_order",
                    "group_id",
                    "option__display_order",
                    "option_id",
                ),
            ),
            active_groups_prefetch(),
        )
        .all()
    )


def cart_payload(cart: Cart) -> dict:
    """Build the public cart representation used by every cart endpoint."""
    items = cart_items_queryset(cart)

    cart_total = Decimal("0.00")
    item_count = 0
    cart_items = []

    for item in items:
        selections = list(item.selected_options.all())
        base_unit_price = item.product.price or Decimal("0.00")
        options_total = sum(
            (
                (selection.option.price_adjustment or Decimal("0.00"))
                for selection in selections
            ),
            Decimal("0.00"),
        )
        configured_unit_price = base_unit_price + options_total
        line_total = configured_unit_price * item.quantity
        cart_total += line_total
        item_count += item.quantity

        cart_items.append(
            {
                "item_id": item.id,
                "product_id": item.product.id,
                "name": item.product.name,
                "product_slug": item.product.slug,
                "quantity": item.quantity,
                "base_unit_price": money(base_unit_price),
                "options_total": money(options_total),
                "configured_unit_price": money(configured_unit_price),
                "price": money(configured_unit_price),
                "line_total": money(line_total),
                "selected_options": [
                    {
                        "group_id": selection.group_id,
                        "group_name": selection.group.name,
                        "option_id": selection.option_id,
                        "option_name": selection.option.name,
                        "price_adjustment": money(
                            selection.option.price_adjustment or Decimal("0.00")
                        ),
                    }
                    for selection in selections
                ],
                "configuration_signature": item.configuration_signature,
                "configuration_valid": configuration_is_valid(item, selections),
                "sku": getattr(item.product, "sku", None),
                "image": featured_image_url(item.product) or "",
            }
        )

    return {
        "id": str(cart.id),
        "items": cart_items,
        "item_count": item_count,
        "total": money(cart_total),
    }


def configuration_is_valid(item, selections=None) -> bool:
    if selections is None:
        selections = list(item.selected_options.all())

    option_ids = [selection.option_id for selection in selections]
    try:
        expected_signature = build_configuration_signature(option_ids)
    except ValueError:
        return False
    if expected_signature != item.configuration_signature:
        return False

    seen_groups = set()
    for selection in selections:
        option = selection.option
        group = selection.group
        if group is None or option is None:
            return False
        if option.group_id != group.id:
            return False
        if group.product_id != item.product_id:
            return False
        if group.id in seen_groups:
            return False
        seen_groups.add(group.id)
        if not group.active or not option.active:
            return False

    required_groups = [
        group
        for group in item.product.option_groups.all()
        if group.required
    ]
    selected_active_groups = {
        selection.group_id
        for selection in selections
        if selection.option.active and selection.group.active
    }
    return all(group.id in selected_active_groups for group in required_groups)


def featured_image_url(product) -> str:
    """Use the prefetched image cache instead of filtering the related manager."""
    images = list(product.productimage_set.all())
    image = next((item for item in images if item.image_type == "thumbnail"), None)
    if image is None:
        image = images[0] if images else None
    if not image or not image.image:
        return ""

    image_url = image.image.url
    if image_url.startswith(("http://", "https://")):
        return image_url
    return f"{settings.SITE_URL}/{image_url.lstrip('/')}"
