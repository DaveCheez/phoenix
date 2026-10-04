"""Validate selected product options for add-to-cart.

`options` must be omitted, JSON null, or a flat list of positive option IDs.
JSON objects are rejected and must not be interpreted.
"""

from decimal import Decimal

from django.db.models import Prefetch

from store.models import ProductOption, ProductOptionGroup

from .configuration import build_configuration_signature
from .exceptions import CartOptionError


FORBIDDEN_PRICE_FIELDS = (
    "price",
    "base_unit_price",
    "unit_price",
    "configured_unit_price",
    "options_total",
    "line_total",
    "cart_total",
    "total",
    "amount",
    "deposit",
    "balance",
)

INVALID_OPTIONS_FORMAT_ERROR = (
    "Your product selections could not be read. "
    "Refresh the product page and select your options again."
)


def reject_browser_prices(payload):
    for field in FORBIDDEN_PRICE_FIELDS:
        if field in payload:
            raise CartOptionError(
                "UNEXPECTED_PRICE_FIELD",
                "Prices are calculated by Phoenix Vanz.",
            )


def parse_requested_options(raw_options):
    """Return a list of strict option IDs. Omitted or null options become []."""
    if raw_options is None:
        return []
    if not isinstance(raw_options, list):
        raise CartOptionError(
            "INVALID_OPTIONS_FORMAT",
            INVALID_OPTIONS_FORMAT_ERROR,
        )
    return parse_strict_option_ids(raw_options)


def parse_strict_option_ids(raw_options):
    parsed = []
    seen = set()
    for value in raw_options:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CartOptionError(
                "INVALID_OPTIONS_FORMAT",
                "Selected options must be a list of option IDs.",
            )
        if value in seen:
            raise CartOptionError(
                "DUPLICATE_OPTION",
                "Each option can only be selected once.",
            )
        seen.add(value)
        parsed.append(value)
    return parsed


def validate_product_options(product, option_ids):
    """Return catalogue options in a stable group order for persistence."""
    active_groups = list(
        ProductOptionGroup.objects.filter(product=product, active=True).order_by(
            "display_order",
            "id",
        )
    )
    if not option_ids:
        _require_required_groups(active_groups, selected_group_ids=set())
        return []

    if not active_groups:
        raise CartOptionError(
            "INVALID_OPTION",
            "One or more selected options are unavailable.",
        )

    options = list(
        ProductOption.objects.select_related("group").filter(id__in=option_ids)
    )
    options_by_id = {option.id: option for option in options}
    if len(options_by_id) != len(set(option_ids)):
        raise CartOptionError(
            "INVALID_OPTION",
            "One or more selected options are unavailable.",
        )

    selected_group_ids = []
    resolved = []
    for option_id in option_ids:
        option = options_by_id[option_id]
        group = option.group
        if (
            group is None
            or not option.active
            or not group.active
            or group.product_id != product.id
            or option.price_adjustment is None
            or option.price_adjustment < Decimal("0.00")
        ):
            raise CartOptionError(
                "INVALID_OPTION",
                "One or more selected options are unavailable.",
            )
        if group.id in selected_group_ids:
            raise CartOptionError(
                "DUPLICATE_GROUP_SELECTION",
                "Choose only one option from each group.",
            )
        selected_group_ids.append(group.id)
        resolved.append(option)

    _require_required_groups(active_groups, selected_group_ids=set(selected_group_ids))
    resolved.sort(
        key=lambda option: (
            option.group.display_order,
            option.group_id,
            option.display_order,
            option.id,
        )
    )
    return resolved


def _require_required_groups(active_groups, selected_group_ids):
    missing = [
        group
        for group in active_groups
        if group.required and group.id not in selected_group_ids
    ]
    if not missing:
        return
    raise CartOptionError(
        "MISSING_REQUIRED_OPTION",
        "Choose an option for every required group.",
        errors={
            "options": [f"Choose an option for {group.name}." for group in missing],
        },
    )


def configuration_signature_for_options(options):
    return build_configuration_signature([option.id for option in options])


def active_groups_prefetch():
    return Prefetch(
        "product__option_groups",
        queryset=ProductOptionGroup.objects.filter(active=True).order_by(
            "display_order",
            "id",
        ),
    )
