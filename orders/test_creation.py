"""Cart-to-order creation tests.

These run on the disposable SQLite test database. They check the service
result and the rollback behaviour. They do not prove PostgreSQL row locks or
concurrent catalogue reads.
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from cart.configuration import build_configuration_signature
from cart.exceptions import CartOptionError
from cart.models import Cart, CartItem, CartItemOption
from store.models import Category, Product, ProductOption

from .creation import (
    REFERENCE_CONSTRAINT_NAME,
    _is_reference_integrity_error,
    _is_reference_only_validation_error,
    create_order_from_cart,
)
from .exceptions import OrderCreationError
from .factories import build_catalogue, create_snapshot_order
from .models import Order, OrderItem, OrderItemOption
from .money import calculate_deposit


def _customer(**overrides):
    values = {
        "customer_name": "Alex Farmer",
        "customer_email": "alex@example.com",
        "customer_phone": "01254 000000",
        "vehicle_make": "Volkswagen",
        "vehicle_model": "T6.1",
        "vehicle_year": 2021,
        "vehicle_wheelbase": "LWB",
    }
    values.update(overrides)
    return values


def _fingerprint(cart):
    cart.refresh_from_db()
    return {
        "updated_at": cart.updated_at,
        "items": [
            {
                "quantity": item.quantity,
                "signature": item.configuration_signature,
                "updated_at": item.updated_at,
                "options": list(
                    item.selected_options.order_by("id").values_list(
                        "id",
                        "group_id",
                        "option_id",
                    )
                ),
            }
            for item in cart.items.order_by("created_at", "id")
        ],
    }


def _add_line(cart, product, options, *, quantity=1, signature=None, group_for=None):
    if signature is None:
        signature = build_configuration_signature([option.id for option in options])
    item = CartItem.objects.create(
        cart=cart,
        product=product,
        quantity=quantity,
        configuration_signature=signature,
    )
    for option in options:
        group = option.group if group_for is None else group_for(option)
        CartItemOption.objects.create(cart_item=item, group=group, option=option)
    return item


class CartToOrderCreationTests(TestCase):
    def test_configured_cart_creates_a_matching_unpaid_snapshot(self):
        catalogue = build_catalogue()
        product = catalogue["product"]
        cart = Cart.objects.create(session_key="configured")
        # Insert the zero-price light before the wheelbase so snapshot order
        # has to follow the catalogue, not the cart-row insert order.
        _add_line(
            cart,
            product,
            [catalogue["no_lights"], catalogue["lwb"]],
            quantity=2,
        )
        product.price = Decimal("700.00")
        product.save()
        catalogue["lwb"].price_adjustment = Decimal("125.00")
        catalogue["lwb"].save()
        before = _fingerprint(cart)

        order = create_order_from_cart(
            cart_id=cart.id,
            vehicle_registration="  ab12 cde  ",
            customer_notes="  side door  ",
            **_customer(customer_name="  Alex Farmer  "),
        )

        self.assertEqual(_fingerprint(cart), before)
        self.assertEqual(order.customer_name, "Alex Farmer")
        self.assertEqual(order.vehicle_registration, "ab12 cde")
        self.assertEqual(order.customer_notes, "side door")
        self.assertTrue(order.is_finalised)
        self.assertEqual(order.amount_paid, Decimal("0.00"))
        self.assertEqual(order.outstanding_balance, order.full_total)
        self.assertEqual(order.payment_status, Order.PAYMENT_PENDING_DEPOSIT)
        self.assertEqual(order.job_status, Order.JOB_NEW)
        self.assertEqual(order.currency, Order.CURRENCY_GBP)
        self.assertEqual(order.fulfilment_method, Order.FULFILMENT_WORKSHOP_FITTING)
        self.assertEqual(order.fitting_charge, Decimal("0.00"))
        self.assertEqual(order.tax_treatment, Order.TAX_NOT_VAT_REGISTERED)
        self.assertEqual(order.tax_amount, Decimal("0.00"))
        self.assertEqual(order.payment_terms, Order.PAYMENT_TERMS_THIRD_DEPOSIT)
        self.assertEqual(order.payment_terms_text, Order.PAYMENT_TERMS_TEXT)
        self.assertEqual(order.source_cart_id, cart.id)

        item = order.items.get()
        self.assertEqual(item.position, 1)
        self.assertEqual(item.sku, "")
        self.assertEqual(item.product_name, "Retro Roof Rack")
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.base_unit_price, Decimal("700.00"))
        self.assertEqual(item.options_total, Decimal("125.00"))
        self.assertEqual(item.configured_unit_price, Decimal("825.00"))
        self.assertEqual(item.line_total, Decimal("1650.00"))
        self.assertEqual(order.full_total, item.line_total)
        split = calculate_deposit(order.full_total)
        self.assertEqual(order.deposit_required, split.deposit)
        self.assertEqual(order.balance_on_completion, split.balance)

        options = list(item.selected_options.order_by("position"))
        self.assertEqual([option.option_name for option in options], ["LWB", "None"])
        self.assertEqual(options[0].option_sku, "LWB")
        self.assertEqual(options[0].price_adjustment, Decimal("125.00"))
        self.assertEqual(options[1].option_sku, "NOLIGHT")
        self.assertEqual(options[1].price_adjustment, Decimal("0.00"))
        self.assertEqual(
            item.options_total,
            sum((option.price_adjustment for option in options), Decimal("0.00")),
        )
        self.assertEqual(
            item.configuration_signature,
            build_configuration_signature([option.original_option_id for option in options]),
        )

        product.name = "Changed after the order"
        product.price = Decimal("10.00")
        product.save()
        catalogue["lwb"].name = "Changed LWB"
        catalogue["lwb"].price_adjustment = Decimal("1.00")
        catalogue["lwb"].save()
        item.refresh_from_db()
        options[0].refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(item.product_name, "Retro Roof Rack")
        self.assertEqual(item.base_unit_price, Decimal("700.00"))
        self.assertEqual(item.line_total, Decimal("1650.00"))
        self.assertEqual(options[0].option_name, "LWB")
        self.assertEqual(options[0].price_adjustment, Decimal("125.00"))
        self.assertEqual(order.full_total, Decimal("1650.00"))

    def test_two_configurations_of_one_product_stay_separate(self):
        catalogue = build_catalogue()
        product = catalogue["product"]
        mwb = ProductOption.objects.create(
            group=catalogue["wheelbase"],
            name="MWB",
            price_adjustment=Decimal("80.00"),
            sku="MWB",
            display_order=20,
        )
        cart = Cart.objects.create(session_key="two-configs")
        _add_line(cart, product, [catalogue["lwb"], catalogue["no_lights"]])
        _add_line(cart, product, [mwb, catalogue["no_lights"]])

        order = create_order_from_cart(cart_id=cart.id, **_customer())
        lines = list(order.items.order_by("position"))
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].original_product_id, lines[1].original_product_id)
        self.assertNotEqual(lines[0].configuration_signature, lines[1].configuration_signature)
        self.assertEqual(
            list(lines[0].selected_options.values_list("option_name", flat=True)),
            ["LWB", "None"],
        )
        self.assertEqual(
            list(lines[1].selected_options.values_list("option_name", flat=True)),
            ["MWB", "None"],
        )
        self.assertEqual(lines[0].line_total, Decimal("825.00"))
        self.assertEqual(lines[1].line_total, Decimal("775.00"))
        self.assertEqual(order.full_total, Decimal("1600.00"))
        self.assertEqual(cart.items.count(), 2)

    def test_two_pound_lines_use_one_whole_order_deposit(self):
        category = Category.objects.create(name="Small", slug="small-parts", type="product")
        first = Product.objects.create(
            category=category,
            name="Bracket",
            slug="bracket",
            price=Decimal("1.00"),
        )
        second = Product.objects.create(
            category=category,
            name="Bolt",
            slug="bolt",
            price=Decimal("1.00"),
        )
        cart = Cart.objects.create(session_key="two-pounds")
        _add_line(cart, first, [])
        _add_line(cart, second, [])

        order = create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertEqual(order.full_total, Decimal("2.00"))
        self.assertEqual(order.deposit_required, Decimal("0.67"))
        self.assertEqual(order.balance_on_completion, Decimal("1.33"))
        self.assertEqual(order.outstanding_balance, Decimal("2.00"))
        self.assertEqual(order.items.count(), 2)

    def test_repeated_calls_are_not_idempotent(self):
        catalogue = build_catalogue()
        cart = Cart.objects.create(session_key="repeat")
        _add_line(cart, catalogue["product"], [catalogue["lwb"], catalogue["no_lights"]])
        first = create_order_from_cart(cart_id=cart.id, **_customer())
        second = create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(Order.objects.filter(source_cart=cart).count(), 2)
        self.assertEqual(cart.items.count(), 1)

    def test_invalid_carts_leave_no_order(self):
        catalogue = build_catalogue()
        product = catalogue["product"]
        cases = {
            "missing": (uuid.uuid4(), OrderCreationError, "CART_NOT_FOUND", None),
        }
        empty = Cart.objects.create(session_key="empty")
        cases["empty"] = (empty.id, OrderCreationError, "EMPTY_CART", empty)

        invalid_quantity = Cart.objects.create(session_key="quantity")
        _add_line(
            invalid_quantity,
            product,
            [catalogue["lwb"]],
            quantity=1000,
        )
        cases["quantity"] = (
            invalid_quantity.id,
            OrderCreationError,
            "INVALID_QUANTITY",
            invalid_quantity,
        )

        missing_required = Cart.objects.create(session_key="required")
        _add_line(missing_required, product, [], signature="")
        cases["required"] = (
            missing_required.id,
            CartOptionError,
            "MISSING_REQUIRED_OPTION",
            missing_required,
        )

        retired = ProductOption.objects.create(
            group=catalogue["wheelbase"],
            name="Retired",
            price_adjustment=Decimal("10.00"),
            sku="RETIRED",
        )
        inactive = Cart.objects.create(session_key="inactive")
        _add_line(inactive, product, [retired])
        retired.active = False
        retired.save()
        cases["inactive"] = (inactive.id, CartOptionError, "INVALID_OPTION", inactive)

        wrong_group = Cart.objects.create(session_key="group")
        _add_line(
            wrong_group,
            product,
            [catalogue["lwb"]],
            group_for=lambda option: catalogue["lights"],
        )
        cases["group"] = (wrong_group.id, CartOptionError, "INVALID_OPTION", wrong_group)

        signature = Cart.objects.create(session_key="signature")
        _add_line(
            signature,
            product,
            [catalogue["lwb"]],
            signature="not-the-stored-signature",
        )
        cases["signature"] = (
            signature.id,
            CartOptionError,
            "CONFIGURATION_CONFLICT",
            signature,
        )

        for cart_id, error_type, code, cart in cases.values():
            before = None if cart is None else _fingerprint(cart)
            orders_before = Order.objects.count()
            with self.assertRaises(error_type) as caught:
                create_order_from_cart(cart_id=cart_id, **_customer())
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("UNIQUE constraint", str(caught.exception))
            self.assertEqual(Order.objects.count(), orders_before)
            self.assertFalse(Order.objects.filter(source_cart_id=cart_id).exists())
            if cart is not None:
                self.assertEqual(_fingerprint(cart), before)

    def test_invalid_customer_and_vehicle_details_leave_no_order(self):
        catalogue = build_catalogue()
        cart = Cart.objects.create(session_key="customer")
        _add_line(cart, catalogue["product"], [catalogue["lwb"]])
        before = _fingerprint(cart)
        rejected = [
            ("INVALID_CUSTOMER", {"customer_name": object()}),
            ("INVALID_CUSTOMER", {"customer_email": "not-an-email"}),
            ("INVALID_CUSTOMER", {"customer_name": "   "}),
            ("INVALID_VEHICLE", {"vehicle_year": True}),
            ("INVALID_VEHICLE", {"vehicle_year": "2021"}),
            ("INVALID_VEHICLE", {"vehicle_year": 1899}),
        ]
        for code, overrides in rejected:
            with self.assertRaises(OrderCreationError) as caught:
                create_order_from_cart(cart_id=cart.id, **_customer(**overrides))
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("not-an-email", caught.exception.message)
            self.assertNotIn("UNIQUE", caught.exception.message)
            self.assertEqual(Order.objects.count(), 0)
            self.assertEqual(_fingerprint(cart), before)

    def test_callers_cannot_supply_prices_or_a_positional_cart(self):
        catalogue = build_catalogue()
        cart = Cart.objects.create(session_key="keywords")
        _add_line(cart, catalogue["product"], [catalogue["lwb"]])
        with self.assertRaises(TypeError):
            create_order_from_cart(cart.id, **_customer())
        with self.assertRaises(TypeError):
            create_order_from_cart(
                cart_id=cart.id,
                full_total=Decimal("1.00"),
                **_customer(),
            )
        self.assertEqual(Order.objects.count(), 0)

    def test_line_or_finalisation_failure_leaves_no_partial_rows(self):
        catalogue = build_catalogue()
        cart = Cart.objects.create(session_key="rollback")
        _add_line(cart, catalogue["product"], [catalogue["lwb"], catalogue["no_lights"]])
        before = _fingerprint(cart)

        real_create = OrderItemOption.objects.create
        calls = {"n": 0}

        def fail_on_second_option(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("snapshot option failed")
            return real_create(*args, **kwargs)

        with patch("orders.creation.OrderItemOption.objects.create", fail_on_second_option):
            with self.assertRaises(RuntimeError):
                create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(OrderItemOption.objects.count(), 0)
        self.assertEqual(_fingerprint(cart), before)

        with patch(
            "orders.creation.Order._mark_finalised",
            side_effect=ValidationError("snapshot mismatch"),
        ):
            with self.assertRaises(ValidationError):
                create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(OrderItemOption.objects.count(), 0)
        self.assertEqual(_fingerprint(cart), before)

    def test_tenth_unit_quantity_three_finalises_thirty_pence(self):
        category = Category.objects.create(name="Small", slug="small-parts", type="product")
        product = Product.objects.create(
            category=category,
            name="Clip",
            slug="small-clip",
            price=Decimal("0.10"),
        )
        cart = Cart.objects.create(session_key="thirty-pence")
        _add_line(cart, product, [], quantity=3, signature="")

        order = create_order_from_cart(cart_id=cart.id, **_customer())

        self.assertTrue(order.is_finalised)
        self.assertEqual(order.full_total, Decimal("0.30"))
        self.assertEqual(order.deposit_required, Decimal("0.10"))
        self.assertEqual(order.balance_on_completion, Decimal("0.20"))
        item = order.items.get()
        self.assertEqual(item.configured_unit_price, Decimal("0.10"))
        self.assertEqual(item.quantity, 3)
        self.assertEqual(item.line_total, Decimal("0.30"))
        self.assertEqual(order.payment_status, "pending_deposit")
        self.assertEqual(order.amount_paid, Decimal("0.00"))


class ReferenceCollisionTests(TestCase):
    def test_reference_validation_collision_retries_then_succeeds(self):
        existing, _catalogue = create_snapshot_order()
        cart = self._plain_cart()
        with patch(
            "orders.creation.generate_order_reference",
            side_effect=[existing.reference, "PV-FRESHORDER01"],
        ):
            order = create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertEqual(order.reference, "PV-FRESHORDER01")
        self.assertEqual(Order.objects.count(), 2)

    def test_reference_collisions_stop_after_the_attempt_limit(self):
        existing, _catalogue = create_snapshot_order()
        cart = self._plain_cart()
        with patch(
            "orders.creation.generate_order_reference",
            return_value=existing.reference,
        ):
            with self.assertRaises(OrderCreationError) as caught:
                create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertEqual(caught.exception.code, "REFERENCE_UNAVAILABLE")
        self.assertNotIn(existing.reference, caught.exception.message)
        self.assertEqual(Order.objects.count(), 1)
        self.assertFalse(OrderItem.objects.exclude(order=existing).exists())

    def test_sqlite_reference_integrity_error_retries_once(self):
        cart = self._plain_cart()
        real_save = Order.save
        calls = {"n": 0}

        def collide_once(order, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("UNIQUE constraint failed: orders_order.reference")
            return real_save(order, *args, **kwargs)

        with patch("orders.creation.Order.save", collide_once):
            order = create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertEqual(calls["n"], 2)
        self.assertTrue(order.is_finalised)
        self.assertEqual(Order.objects.count(), 1)

    def test_unrelated_validation_and_integrity_errors_are_not_retried(self):
        cart = self._plain_cart()
        calls = {"n": 0}

        def invalid(order, *args, **kwargs):
            calls["n"] += 1
            raise ValidationError({"reference": "taken", "full_total": "bad"})

        with patch("orders.creation.Order.save", invalid):
            with self.assertRaises(ValidationError):
                create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(Order.objects.count(), 0)

        calls["n"] = 0

        def wrong_reference_code(order, *args, **kwargs):
            calls["n"] += 1
            raise ValidationError(
                {"reference": [ValidationError("too long", code="max_length")]}
            )

        with patch("orders.creation.Order.save", wrong_reference_code):
            with self.assertRaises(ValidationError):
                create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(Order.objects.count(), 0)

        calls["n"] = 0

        def other_constraint(order, *args, **kwargs):
            calls["n"] += 1
            raise IntegrityError(
                "UNIQUE constraint failed: orders_orderitem.order_id, orders_orderitem.position"
            )

        with patch("orders.creation.Order.save", other_constraint):
            with self.assertRaises(IntegrityError):
                create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(Order.objects.count(), 0)

        calls["n"] = 0

        def unknown(order, *args, **kwargs):
            calls["n"] += 1
            raise IntegrityError("database is locked")

        with patch("orders.creation.Order.save", unknown):
            with self.assertRaises(IntegrityError):
                create_order_from_cart(cart_id=cart.id, **_customer())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(Order.objects.count(), 0)

    def test_reference_collision_detector_is_narrow(self):
        """Mocked psycopg2 metadata. This does not execute PostgreSQL."""
        self.assertEqual(REFERENCE_CONSTRAINT_NAME, "orders_order_reference_key")
        self.assertTrue(
            _is_reference_integrity_error(
                IntegrityError("UNIQUE constraint failed: orders_order.reference")
            )
        )
        self.assertFalse(
            _is_reference_integrity_error(
                IntegrityError(
                    "UNIQUE constraint failed: orders_orderitem.order_id, orders_orderitem.position"
                )
            )
        )
        self.assertFalse(
            _is_reference_integrity_error(IntegrityError("database is locked"))
        )
        self.assertFalse(
            _is_reference_integrity_error(
                IntegrityError(
                    'duplicate key value violates unique constraint "orders_order_reference_key"'
                )
            )
        )
        self.assertFalse(
            _is_reference_only_validation_error(
                ValidationError({"reference": "taken", "customer_name": "bad"})
            )
        )
        self.assertFalse(
            _is_reference_only_validation_error(ValidationError({"reference": "taken"}))
        )
        self.assertFalse(
            _is_reference_only_validation_error(
                ValidationError({"reference": [ValidationError("too long", code="max_length")]})
            )
        )
        self.assertTrue(
            _is_reference_only_validation_error(
                ValidationError({"reference": [ValidationError("taken", code="unique")]})
            )
        )

        def driver_error(*, pgcode=None, constraint=None, table=None, sqlstate=None):
            class Cause(Exception):
                def __init__(self):
                    self.pgcode = pgcode
                    self.sqlstate = sqlstate
                    self.diag = type(
                        "Diag",
                        (),
                        {
                            "constraint_name": constraint,
                            "table_name": table,
                            "sqlstate": sqlstate,
                        },
                    )()

            error = IntegrityError("duplicate key value violates unique constraint")
            error.__cause__ = Cause()
            return error

        self.assertTrue(
            _is_reference_integrity_error(
                driver_error(
                    pgcode="23505",
                    constraint="orders_order_reference_key",
                    table="orders_order",
                )
            )
        )
        self.assertTrue(
            _is_reference_integrity_error(
                driver_error(pgcode="23505", constraint="orders_order_reference_key")
            )
        )
        self.assertFalse(
            _is_reference_integrity_error(
                driver_error(pgcode="23505", constraint="orders_order_pkey", table="orders_order")
            )
        )
        self.assertFalse(
            _is_reference_integrity_error(
                driver_error(
                    pgcode="23503",
                    constraint="orders_order_reference_key",
                    table="orders_order",
                )
            )
        )
        self.assertFalse(
            _is_reference_integrity_error(
                driver_error(pgcode="23505", constraint="orders_order_reference_key", table="orders_orderitem")
            )
        )
        self.assertFalse(
            _is_reference_integrity_error(driver_error(pgcode="23505"))
        )
        self.assertFalse(
            _is_reference_integrity_error(
                driver_error(constraint="orders_order_reference_key", table="orders_order")
            )
        )
        hashed = IntegrityError("duplicate key")
        hashed.__cause__ = type("Cause", (Exception,), {})()
        hashed.__cause__.pgcode = "23505"
        hashed.__cause__.diag = type(
            "Diag",
            (),
            {"constraint_name": "orders_order_reference_a1b2c3d4_uniq", "table_name": "orders_order"},
        )()
        self.assertFalse(_is_reference_integrity_error(hashed))

    def test_real_sqlite_reference_insert_uses_the_column_error(self):
        if connection.vendor != "sqlite":
            self.skipTest(
                "This assertion is the SQLite column error. PostgreSQL uses the constraint name."
            )
        existing, _catalogue = create_snapshot_order()
        values = {}
        for field in existing._meta.concrete_fields:
            if field.primary_key:
                continue
            values[field.attname] = getattr(existing, field.attname)
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                Order.objects.bulk_create([Order(id=uuid.uuid4(), **values)])
        self.assertTrue(_is_reference_integrity_error(caught.exception))
        self.assertIn("orders_order.reference", str(caught.exception))
        self.assertEqual(Order.objects.count(), 1)

    def _plain_cart(self):
        category = Category.objects.create(name="Plain", slug="plain-parts", type="product")
        product = Product.objects.create(
            category=category,
            name="Cap",
            slug="plain-cap",
            price=Decimal("1.00"),
        )
        cart = Cart.objects.create(session_key="plain")
        _add_line(cart, product, [])
        return cart
