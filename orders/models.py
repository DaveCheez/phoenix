"""Persistent snapshots of agreed Phoenix Vanz orders.

These models store names, prices and terms as they were at creation. Ordinary
``save()`` calls do not refresh values from live catalogue rows.

Limits of this protection:
- ``Model.save()`` and ``full_clean()`` reject ordinary edits to snapshot
  fields.
- ``QuerySet.update()``, raw SQL and database shells are not intercepted.
- Later payment work must use a dedicated, audited service for
  ``amount_paid`` and payment status. Do not add a generic bypass flag here.
"""

import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q

from cart.models import Cart
from store.models import Product, ProductOption, ProductOptionGroup

from .money import OrderMoneyError, calculate_deposit


MONEY = dict(max_digits=12, decimal_places=2)
MAX_ORDER_QUANTITY = 999


def generate_order_reference() -> str:
    return f"PV-{uuid.uuid4().hex[:12].upper()}"


class Order(models.Model):
    CURRENCY_GBP = "GBP"
    FULFILMENT_WORKSHOP_FITTING = "workshop_fitting"
    TAX_NOT_VAT_REGISTERED = "not_vat_registered"
    PAYMENT_TERMS_THIRD_DEPOSIT = "one_third_deposit_balance_on_completion"
    PAYMENT_TERMS_TEXT = (
        "One-third deposit. Balance due on completion of the work and fitting."
    )
    PAYMENT_PENDING_DEPOSIT = "pending_deposit"
    JOB_NEW = "new"

    SNAPSHOT_FIELDS = (
        "reference",
        "customer_name",
        "customer_email",
        "customer_phone",
        "vehicle_make",
        "vehicle_model",
        "vehicle_year",
        "vehicle_wheelbase",
        "vehicle_registration",
        "customer_notes",
        "currency",
        "full_total",
        "deposit_required",
        "balance_on_completion",
        "amount_paid",
        "fulfilment_method",
        "fitting_charge",
        "tax_treatment",
        "tax_amount",
        "payment_terms",
        "payment_terms_text",
        "payment_status",
        "job_status",
        "source_cart_id",
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=32, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    customer_name = models.CharField(max_length=200)
    customer_email = models.EmailField()
    customer_phone = models.CharField(max_length=40)
    vehicle_make = models.CharField(max_length=100)
    vehicle_model = models.CharField(max_length=100)
    vehicle_year = models.PositiveIntegerField(
        validators=[MinValueValidator(1900), MaxValueValidator(2100)]
    )
    vehicle_wheelbase = models.CharField(max_length=50)
    vehicle_registration = models.CharField(max_length=20, blank=True)
    customer_notes = models.TextField(blank=True)

    currency = models.CharField(max_length=3, default=CURRENCY_GBP)
    full_total = models.DecimalField(
        **MONEY,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    deposit_required = models.DecimalField(**MONEY)
    balance_on_completion = models.DecimalField(**MONEY)
    amount_paid = models.DecimalField(**MONEY, default=Decimal("0.00"))

    fulfilment_method = models.CharField(
        max_length=32,
        default=FULFILMENT_WORKSHOP_FITTING,
    )
    fitting_charge = models.DecimalField(**MONEY, default=Decimal("0.00"))
    tax_treatment = models.CharField(
        max_length=32,
        default=TAX_NOT_VAT_REGISTERED,
    )
    tax_amount = models.DecimalField(**MONEY, default=Decimal("0.00"))
    payment_terms = models.CharField(
        max_length=80,
        default=PAYMENT_TERMS_THIRD_DEPOSIT,
    )
    payment_terms_text = models.CharField(
        max_length=255,
        default=PAYMENT_TERMS_TEXT,
    )
    payment_status = models.CharField(
        max_length=32,
        default=PAYMENT_PENDING_DEPOSIT,
    )
    job_status = models.CharField(max_length=32, default=JOB_NEW)
    internal_notes = models.TextField(blank=True)

    source_cart = models.ForeignKey(
        Cart,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="snapshot_orders",
    )

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.CheckConstraint(
                check=Q(full_total__gt=0),
                name="order_full_total_positive",
            ),
            models.CheckConstraint(
                check=Q(deposit_required__gte=0)
                & Q(balance_on_completion__gte=0)
                & Q(amount_paid__gte=0)
                & Q(fitting_charge__gte=0)
                & Q(tax_amount__gte=0),
                name="order_money_non_negative",
            ),
            models.CheckConstraint(
                check=Q(amount_paid__lte=F("full_total")),
                name="order_amount_paid_lte_full_total",
            ),
            models.CheckConstraint(
                check=Q(
                    deposit_required=F("full_total") - F("balance_on_completion")
                ),
                name="order_deposit_plus_balance_equals_total",
            ),
        ]

    def __str__(self):
        return self.reference

    @property
    def outstanding_balance(self):
        paid = self.amount_paid if self.amount_paid is not None else Decimal("0.00")
        return self.full_total - paid

    def clean(self):
        super().clean()
        errors = {}
        if self.currency != self.CURRENCY_GBP:
            errors["currency"] = "Orders are stored in GBP."
        if self.fulfilment_method != self.FULFILMENT_WORKSHOP_FITTING:
            errors["fulfilment_method"] = "Orders are completed by workshop fitting."
        if self.tax_treatment != self.TAX_NOT_VAT_REGISTERED:
            errors["tax_treatment"] = "Phoenix Vanz is not VAT registered."
        if self.fitting_charge != Decimal("0.00"):
            errors["fitting_charge"] = "Workshop fitting is included."
        if self.tax_amount != Decimal("0.00"):
            errors["tax_amount"] = "No VAT is charged."
        if self.payment_terms != self.PAYMENT_TERMS_THIRD_DEPOSIT:
            errors["payment_terms"] = "Orders use a one-third deposit."
        if self.full_total is not None:
            try:
                split = calculate_deposit(self.full_total)
            except OrderMoneyError as exc:
                errors["full_total"] = str(exc)
            else:
                if self.deposit_required != split.deposit:
                    errors["deposit_required"] = (
                        "Deposit must be exactly one-third of the order total."
                    )
                if self.balance_on_completion != split.balance:
                    errors["balance_on_completion"] = (
                        "Balance must be the remainder after the deposit."
                    )
                if (
                    self.deposit_required is not None
                    and self.balance_on_completion is not None
                    and self.deposit_required + self.balance_on_completion
                    != self.full_total
                ):
                    errors["full_total"] = (
                        "Deposit and balance must equal the order total."
                    )
        if self.amount_paid is not None and self.full_total is not None:
            if self.amount_paid < Decimal("0.00") or self.amount_paid > self.full_total:
                errors["amount_paid"] = "Amount paid must be between zero and the total."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self._state.adding and not self.reference:
            self.reference = generate_order_reference()
        if not self._state.adding:
            self._assert_snapshot_unchanged(kwargs.get("update_fields"))
        self.full_clean()
        super().save(*args, **kwargs)

    def _assert_snapshot_unchanged(self, update_fields):
        allowed = {"internal_notes", "updated_at"}
        if update_fields is not None:
            forbidden = set(update_fields) - allowed
            if forbidden:
                raise ValidationError(
                    "Order snapshots cannot be rewritten after creation."
                )
        persisted = type(self).objects.get(pk=self.pk)
        for field_name in self.SNAPSHOT_FIELDS:
            if getattr(self, field_name) != getattr(persisted, field_name):
                raise ValidationError(
                    "Order snapshots cannot be rewritten after creation."
                )


class OrderItem(models.Model):
    SNAPSHOT_FIELDS = (
        "order_id",
        "position",
        "original_product_id",
        "product_id",
        "product_name",
        "product_slug",
        "sku",
        "configuration_signature",
        "quantity",
        "base_unit_price",
        "options_total",
        "configured_unit_price",
        "line_total",
    )

    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    original_product_id = models.PositiveIntegerField()
    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_item_snapshots",
    )
    product_name = models.CharField(max_length=200)
    product_slug = models.SlugField(max_length=50)
    sku = models.CharField(max_length=50, blank=True)
    configuration_signature = models.CharField(max_length=512)
    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(MAX_ORDER_QUANTITY)]
    )
    base_unit_price = models.DecimalField(**MONEY)
    options_total = models.DecimalField(**MONEY)
    configured_unit_price = models.DecimalField(**MONEY)
    line_total = models.DecimalField(**MONEY)

    class Meta:
        ordering = ("position", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("order", "position"),
                name="unique_position_per_order",
            ),
            models.CheckConstraint(
                check=Q(quantity__gte=1) & Q(quantity__lte=MAX_ORDER_QUANTITY),
                name="orderitem_quantity_1_to_999",
            ),
            models.CheckConstraint(
                check=Q(base_unit_price__gte=0)
                & Q(options_total__gte=0)
                & Q(configured_unit_price__gte=0)
                & Q(line_total__gte=0),
                name="orderitem_money_non_negative",
            ),
            models.CheckConstraint(
                check=Q(
                    configured_unit_price=F("base_unit_price") + F("options_total")
                ),
                name="orderitem_configured_unit_matches_parts",
            ),
            models.CheckConstraint(
                check=Q(line_total=F("configured_unit_price") * F("quantity")),
                name="orderitem_line_total_matches_quantity",
            ),
        ]

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"

    def clean(self):
        super().clean()
        errors = {}
        if (
            self.base_unit_price is not None
            and self.options_total is not None
            and self.configured_unit_price is not None
            and self.configured_unit_price
            != self.base_unit_price + self.options_total
        ):
            errors["configured_unit_price"] = (
                "Configured unit price must equal the base price plus options."
            )
        if (
            self.configured_unit_price is not None
            and self.quantity is not None
            and self.line_total is not None
            and self.line_total != self.configured_unit_price * self.quantity
        ):
            errors["line_total"] = (
                "Line total must equal the configured unit price times quantity."
            )
        for field_name in (
            "base_unit_price",
            "options_total",
            "configured_unit_price",
            "line_total",
        ):
            value = getattr(self, field_name)
            if value is not None and value < Decimal("0.00"):
                errors[field_name] = "Money values cannot be negative."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Order item snapshots cannot be changed.")
        self.full_clean()
        super().save(*args, **kwargs)


class OrderItemOption(models.Model):
    SNAPSHOT_FIELDS = (
        "order_item_id",
        "position",
        "original_group_id",
        "original_option_id",
        "group_id",
        "option_id",
        "group_name",
        "option_name",
        "option_sku",
        "price_adjustment",
    )

    order_item = models.ForeignKey(
        OrderItem,
        related_name="selected_options",
        on_delete=models.CASCADE,
    )
    position = models.PositiveIntegerField()
    original_group_id = models.PositiveIntegerField()
    original_option_id = models.PositiveIntegerField()
    group = models.ForeignKey(
        ProductOptionGroup,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_item_option_snapshots",
    )
    option = models.ForeignKey(
        ProductOption,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_item_option_snapshots",
    )
    group_name = models.CharField(max_length=100)
    option_name = models.CharField(max_length=100)
    option_sku = models.CharField(max_length=50, blank=True)
    price_adjustment = models.DecimalField(**MONEY)

    class Meta:
        ordering = ("position", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("order_item", "position"),
                name="unique_option_position_per_order_item",
            ),
            models.UniqueConstraint(
                fields=("order_item", "original_group_id"),
                name="unique_group_per_order_item",
            ),
            models.UniqueConstraint(
                fields=("order_item", "original_option_id"),
                name="unique_option_per_order_item",
            ),
            models.CheckConstraint(
                check=Q(price_adjustment__gte=0),
                name="orderitemoption_price_adjustment_gte_0",
            ),
        ]

    def __str__(self):
        return f"{self.group_name}: {self.option_name}"

    def clean(self):
        super().clean()
        if self.price_adjustment is not None and self.price_adjustment < Decimal("0.00"):
            raise ValidationError(
                {"price_adjustment": "Price adjustments cannot be negative."}
            )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Selected-option snapshots cannot be changed.")
        self.full_clean()
        super().save(*args, **kwargs)
