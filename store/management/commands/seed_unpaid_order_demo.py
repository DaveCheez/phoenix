"""Idempotent local demo product for unpaid-order manual testing.

This writes only to the dedicated local SQLite file
``local-unpaid-order.sqlite3``. Any other database is refused.
"""

from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from store.models import Category, Product, ProductOption, ProductOptionGroup


DEMO_SLUG = "unpaid-order-demo"
DEDICATED_FILENAME = "local-unpaid-order.sqlite3"
_REFUSED = "Refusing to seed this database."


class Command(BaseCommand):
    help = "Create or update the local unpaid-order demo product."

    def handle(self, *args, **options):
        _assert_dedicated_local_database()
        with transaction.atomic():
            _seed()
        self.stdout.write("local order demo catalogue ready")


def _assert_dedicated_local_database():
    database = settings.DATABASES["default"]
    engine = str(database.get("ENGINE") or "")
    name = str(database.get("NAME") or "")
    host = str(database.get("HOST") or "")
    folded = f"{name} {host}".casefold()
    if "neon" in folded or "phoenixvanz.com" in folded or "ondigitalocean" in folded:
        raise CommandError(_REFUSED)
    if not engine.endswith("sqlite3"):
        raise CommandError(_REFUSED)
    if not name or name == ":memory:":
        raise CommandError(_REFUSED)
    if Path(name).name != DEDICATED_FILENAME:
        raise CommandError(_REFUSED)


def _seed():
    category, _created = Category.objects.get_or_create(
        slug=DEMO_SLUG,
        defaults={"name": "Unpaid order demo", "type": "product"},
    )
    category.name = "Unpaid order demo"
    category.type = "product"
    category.save(update_fields=["name", "type"])

    product = Product.objects.filter(slug=DEMO_SLUG).order_by("id").first()
    if product is None:
        product = Product(
            category=category,
            name="Unpaid Order Demo",
            slug=DEMO_SLUG,
            description="Local demo product for unpaid order review.",
            price=Decimal("100.00"),
        )
    product.category = category
    product.name = "Unpaid Order Demo"
    product.description = "Local demo product for unpaid order review."
    product.price = Decimal("100.00")
    product.save()

    specification = _group(product, "Specification", required=True, display_order=10)
    extra = _group(product, "Extra", required=False, display_order=20)
    _option(
        specification,
        "Standard",
        Decimal("0.00"),
        display_order=10,
        is_default=True,
    )
    _option(
        specification,
        "Enhanced",
        Decimal("25.00"),
        display_order=20,
        is_default=False,
    )
    _option(extra, "None", Decimal("0.00"), display_order=10, is_default=True)

    side_rail = _product(
        category,
        slug="side-rail-demo",
        name="Side Rail Demo",
        description="Local demo rail for a second order line.",
        price=Decimal("240.00"),
    )
    length = _group(side_rail, "Length", required=True, display_order=10)
    finish = _group(side_rail, "Finish", required=False, display_order=20)
    _option(length, "SWB", Decimal("0.00"), display_order=10, is_default=True)
    _option(length, "LWB", Decimal("35.00"), display_order=20, is_default=False)
    _option(finish, "None", Decimal("0.00"), display_order=10, is_default=True)

    bulkhead = _product(
        category,
        slug="bulkhead-demo",
        name="Bulkhead Demo",
        description="Local demo bulkhead for a lower-priced order line.",
        price=Decimal("75.00"),
    )
    material = _group(bulkhead, "Material", required=True, display_order=10)
    fixings = _group(bulkhead, "Fixings", required=False, display_order=20)
    _option(material, "Standard", Decimal("0.00"), display_order=10, is_default=True)
    _option(material, "Insulated", Decimal("45.00"), display_order=20, is_default=False)
    _option(fixings, "None", Decimal("0.00"), display_order=10, is_default=True)


def _product(category, *, slug, name, description, price):
    product = Product.objects.filter(slug=slug).order_by("id").first()
    if product is None:
        product = Product(category=category, name=name, slug=slug, description=description, price=price)
    product.category = category
    product.name = name
    product.description = description
    product.price = price
    product.save()
    return product


def _group(product, name, *, required, display_order):
    group = ProductOptionGroup.objects.filter(product=product, name=name).order_by("id").first()
    if group is None:
        group = ProductOptionGroup(product=product, name=name)
    group.required = required
    group.display_order = display_order
    group.active = True
    group.save()
    return group


def _option(group, name, price, *, display_order, is_default):
    option = ProductOption.objects.filter(group=group, name=name).order_by("id").first()
    if is_default:
        ProductOption.objects.filter(group=group, is_default=True).exclude(
            pk=getattr(option, "pk", None)
        ).update(is_default=False)
    if option is None:
        option = ProductOption(group=group, name=name)
    option.price_adjustment = price
    option.display_order = display_order
    option.active = True
    option.is_default = is_default
    option.save()
    return option
