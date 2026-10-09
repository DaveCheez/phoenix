"""Database money-constraint tests.

Rows are inserted with ``bulk_create()`` so ``full_clean()`` and ``save()``
do not reject them first. The disposable test database enforces the checks.
"""

import uuid
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from .models import Order, OrderItem


def _insert_order(*, full_total, deposit_required, balance_on_completion):
    now = timezone.now()
    order = Order(
        id=uuid.uuid4(),
        reference="PV-" + uuid.uuid4().hex[:16].upper(),
        created_at=now,
        updated_at=now,
        customer_name="Alex Farmer",
        customer_email="alex@example.com",
        customer_phone="01254 000000",
        vehicle_make="Volkswagen",
        vehicle_model="T6.1",
        vehicle_year=2021,
        vehicle_wheelbase="LWB",
        currency=Order.CURRENCY_GBP,
        full_total=full_total,
        deposit_required=deposit_required,
        balance_on_completion=balance_on_completion,
        amount_paid=Decimal("0.00"),
        fulfilment_method=Order.FULFILMENT_WORKSHOP_FITTING,
        fitting_charge=Decimal("0.00"),
        tax_treatment=Order.TAX_NOT_VAT_REGISTERED,
        tax_amount=Decimal("0.00"),
        payment_terms=Order.PAYMENT_TERMS_THIRD_DEPOSIT,
        payment_terms_text=Order.PAYMENT_TERMS_TEXT,
        payment_status=Order.PAYMENT_PENDING_DEPOSIT,
        job_status=Order.JOB_NEW,
        is_finalised=False,
    )
    Order.objects.bulk_create([order])
    return order


def _insert_item(
    order,
    *,
    position,
    base_unit_price,
    options_total,
    configured_unit_price,
    quantity,
    line_total,
    product_name="Clip",
    product_slug="clip",
):
    item = OrderItem(
        order=order,
        position=position,
        original_product_id=position,
        product_name=product_name,
        product_slug=product_slug,
        sku="",
        configuration_signature="",
        quantity=quantity,
        base_unit_price=base_unit_price,
        options_total=options_total,
        configured_unit_price=configured_unit_price,
        line_total=line_total,
    )
    OrderItem.objects.bulk_create([item])
    return item


class DatabaseMoneyConstraintTests(TestCase):
    def test_valid_penny_rows_are_accepted(self):
        paid = _insert_order(
            full_total=Decimal("825.00"),
            deposit_required=Decimal("275.00"),
            balance_on_completion=Decimal("550.00"),
        )
        _insert_item(
            paid,
            position=1,
            base_unit_price=Decimal("695.00"),
            options_total=Decimal("130.00"),
            configured_unit_price=Decimal("825.00"),
            quantity=1,
            line_total=Decimal("825.00"),
            product_name="Retro Roof Rack",
            product_slug="retro-roof-rack",
        )
        _insert_item(
            paid,
            position=2,
            base_unit_price=Decimal("0.10"),
            options_total=Decimal("0.20"),
            configured_unit_price=Decimal("0.30"),
            quantity=1,
            line_total=Decimal("0.30"),
        )

        multiples = _insert_order(
            full_total=Decimal("1.32"),
            deposit_required=Decimal("0.44"),
            balance_on_completion=Decimal("0.88"),
        )
        _insert_item(
            multiples,
            position=1,
            base_unit_price=Decimal("0.10"),
            options_total=Decimal("0.00"),
            configured_unit_price=Decimal("0.10"),
            quantity=3,
            line_total=Decimal("0.30"),
        )
        _insert_item(
            multiples,
            position=2,
            base_unit_price=Decimal("0.05"),
            options_total=Decimal("0.00"),
            configured_unit_price=Decimal("0.05"),
            quantity=3,
            line_total=Decimal("0.15"),
        )
        _insert_item(
            multiples,
            position=3,
            base_unit_price=Decimal("0.29"),
            options_total=Decimal("0.00"),
            configured_unit_price=Decimal("0.29"),
            quantity=3,
            line_total=Decimal("0.87"),
        )

        above_32_bit = _insert_order(
            full_total=Decimal("21474836.48"),
            deposit_required=Decimal("0.01"),
            balance_on_completion=Decimal("21474836.47"),
        )
        _insert_item(
            above_32_bit,
            position=1,
            base_unit_price=Decimal("21474836.48"),
            options_total=Decimal("0.00"),
            configured_unit_price=Decimal("21474836.48"),
            quantity=1,
            line_total=Decimal("21474836.48"),
            product_name="Large rack",
            product_slug="large-rack",
        )

        ceiling = _insert_order(
            full_total=Decimal("9999999999.99"),
            deposit_required=Decimal("3333333333.33"),
            balance_on_completion=Decimal("6666666666.66"),
        )
        _insert_item(
            ceiling,
            position=1,
            base_unit_price=Decimal("9999999999.99"),
            options_total=Decimal("0.00"),
            configured_unit_price=Decimal("9999999999.99"),
            quantity=1,
            line_total=Decimal("9999999999.99"),
            product_name="Ceiling",
            product_slug="ceiling",
        )

        self.assertEqual(Order.objects.count(), 4)
        self.assertEqual(paid.items.count(), 2)
        self.assertEqual(multiples.items.count(), 3)

    def test_deposit_identity_accepts_listed_splits(self):
        splits = (
            (Decimal("0.29"), Decimal("0.10"), Decimal("0.19")),
            (Decimal("2.00"), Decimal("0.67"), Decimal("1.33")),
            (Decimal("100.00"), Decimal("33.33"), Decimal("66.67")),
            (Decimal("100.01"), Decimal("33.34"), Decimal("66.67")),
            (Decimal("100.02"), Decimal("33.34"), Decimal("66.68")),
            (Decimal("9999999999.99"), Decimal("3333333333.33"), Decimal("6666666666.66")),
        )
        for full_total, deposit_required, balance_on_completion in splits:
            _insert_order(
                full_total=full_total,
                deposit_required=deposit_required,
                balance_on_completion=balance_on_completion,
            )
        self.assertEqual(Order.objects.count(), len(splits))

    def test_deposit_identity_rejects_one_penny_either_way(self):
        before = Order.objects.count()
        for deposit_required, balance_on_completion in (
            (Decimal("0.68"), Decimal("1.33")),
            (Decimal("0.66"), Decimal("1.33")),
        ):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    _insert_order(
                        full_total=Decimal("2.00"),
                        deposit_required=deposit_required,
                        balance_on_completion=balance_on_completion,
                    )
        self.assertEqual(Order.objects.count(), before)

    def test_configured_price_identity_rejects_one_penny_either_way(self):
        order = _insert_order(
            full_total=Decimal("2.00"),
            deposit_required=Decimal("0.67"),
            balance_on_completion=Decimal("1.33"),
        )
        before = order.items.count()
        for configured_unit_price in (Decimal("0.31"), Decimal("0.29")):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    _insert_item(
                        order,
                        position=1,
                        base_unit_price=Decimal("0.10"),
                        options_total=Decimal("0.20"),
                        configured_unit_price=configured_unit_price,
                        quantity=1,
                        line_total=configured_unit_price,
                    )
        self.assertEqual(order.items.count(), before)

    def test_line_total_identity_rejects_one_penny_either_way(self):
        order = _insert_order(
            full_total=Decimal("2.00"),
            deposit_required=Decimal("0.67"),
            balance_on_completion=Decimal("1.33"),
        )
        before = order.items.count()
        for line_total in (Decimal("0.31"), Decimal("0.29")):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    _insert_item(
                        order,
                        position=1,
                        base_unit_price=Decimal("0.10"),
                        options_total=Decimal("0.00"),
                        configured_unit_price=Decimal("0.10"),
                        quantity=3,
                        line_total=line_total,
                    )
        self.assertEqual(order.items.count(), before)
