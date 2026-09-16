from decimal import Decimal

from .models import Cart


def money(value: Decimal) -> str:
    """Return a stable two-decimal currency string for API responses."""
    return format(value.quantize(Decimal("0.01")), ".2f")


def cart_payload(cart: Cart) -> dict:
    """Build the public cart representation used by every cart endpoint."""
    items = (
        cart.items.select_related("product")
        .prefetch_related("product__productimage_set")
        .all()
    )

    cart_total = Decimal("0.00")
    item_count = 0
    cart_items = []

    for item in items:
        unit_price = item.product.price or Decimal("0.00")
        line_total = unit_price * item.quantity
        cart_total += line_total
        item_count += item.quantity

        cart_items.append(
            {
                "item_id": item.id,
                "product_id": item.product.id,
                "name": item.product.name,
                "product_slug": item.product.slug,
                "quantity": item.quantity,
                "price": money(unit_price),
                "line_total": money(line_total),
                "sku": getattr(item.product, "sku", None),
                "image": item.product.get_featured_image_url() or "",
            }
        )

    return {
        "id": str(cart.id),
        "items": cart_items,
        "item_count": item_count,
        "total": money(cart_total),
    }
