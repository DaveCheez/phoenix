"""Local test fixtures for internally consistent order snapshots.

This module is not a public API and is not a production seeding command.
"""

from decimal import Decimal

from cart.configuration import build_configuration_signature
from store.models import Category, Product, ProductOption, ProductOptionGroup

from .models import Order, OrderItem, OrderItemOption
from .money import calculate_deposit


def build_catalogue():
    category = Category.objects.create(
        name="Roof Racks",
        slug="roof-racks-orders",
        type="product",
    )
    product = Product.objects.create(
        category=category,
        name="Retro Roof Rack",
        slug="retro-roof-rack-orders",
        price=Decimal("695.00"),
    )
    wheelbase = ProductOptionGroup.objects.create(
        product=product,
        name="Wheelbase",
        required=True,
        display_order=10,
    )
    lights = ProductOptionGroup.objects.create(
        product=product,
        name="Lights",
        required=False,
        display_order=20,
    )
    lwb = ProductOption.objects.create(
        group=wheelbase,
        name="LWB",
        price_adjustment=Decimal("130.00"),
        sku="LWB",
        display_order=10,
    )
    no_lights = ProductOption.objects.create(
        group=lights,
        name="None",
        price_adjustment=Decimal("0.00"),
        sku="NOLIGHT",
        display_order=10,
        is_default=True,
    )
    return {
        "category": category,
        "product": product,
        "wheelbase": wheelbase,
        "lights": lights,
        "lwb": lwb,
        "no_lights": no_lights,
    }


def create_snapshot_order(*, quantity=1, source_cart=None, finalise=True, **order_overrides):
    catalogue = build_catalogue()
    product = catalogue["product"]
    lwb = catalogue["lwb"]
    no_lights = catalogue["no_lights"]

    base_unit_price = product.price
    options_total = lwb.price_adjustment + no_lights.price_adjustment
    configured_unit_price = base_unit_price + options_total
    line_total = configured_unit_price * quantity
    total = line_total
    split = calculate_deposit(total)

    defaults = {
        "customer_name": "Alex Farmer",
        "customer_email": "alex@example.com",
        "customer_phone": "01254 000000",
        "vehicle_make": "Volkswagen",
        "vehicle_model": "T6.1",
        "vehicle_year": 2021,
        "vehicle_wheelbase": "LWB",
        "full_total": total,
        "deposit_required": split.deposit,
        "balance_on_completion": split.balance,
        "source_cart": source_cart,
    }
    defaults.update(order_overrides)
    order = Order.objects.create(**defaults)

    item = OrderItem.objects.create(
        order=order,
        position=1,
        original_product_id=product.id,
        product=product,
        product_name=product.name,
        product_slug=product.slug,
        sku="",
        configuration_signature=build_configuration_signature([lwb.id, no_lights.id]),
        quantity=quantity,
        base_unit_price=base_unit_price,
        options_total=options_total,
        configured_unit_price=configured_unit_price,
        line_total=line_total,
    )
    OrderItemOption.objects.create(
        order_item=item,
        position=1,
        original_group_id=catalogue["wheelbase"].id,
        original_option_id=lwb.id,
        group=catalogue["wheelbase"],
        option=lwb,
        group_name="Wheelbase",
        option_name="LWB",
        option_sku=lwb.sku,
        price_adjustment=lwb.price_adjustment,
    )
    OrderItemOption.objects.create(
        order_item=item,
        position=2,
        original_group_id=catalogue["lights"].id,
        original_option_id=no_lights.id,
        group=catalogue["lights"],
        option=no_lights,
        group_name="Lights",
        option_name="None",
        option_sku=no_lights.sku,
        price_adjustment=Decimal("0.00"),
    )
    if finalise:
        order = order._mark_finalised()
    return order, catalogue
