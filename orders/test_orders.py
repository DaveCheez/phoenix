from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from cart.models import Cart, CartItem
from store.models import Category, Product, ProductOption, ProductOptionGroup

from .factories import create_snapshot_order
from .models import Order, OrderItem, OrderItemOption
from .money import calculate_deposit


class OrderSnapshotTests(TestCase):
    def test_outstanding_balance_equals_full_total_when_unpaid(self):
        order, _catalogue = create_snapshot_order()
        self.assertEqual(order.amount_paid, Decimal("0.00"))
        self.assertEqual(order.outstanding_balance, order.full_total)
        self.assertEqual(order.outstanding_balance, Decimal("825.00"))

    def test_new_order_is_unpaid_and_does_not_imply_payment(self):
        order, _catalogue = create_snapshot_order()
        self.assertEqual(order.payment_status, Order.PAYMENT_PENDING_DEPOSIT)
        self.assertEqual(order.job_status, Order.JOB_NEW)
        self.assertEqual(order.amount_paid, Decimal("0.00"))
        self.assertEqual(order.currency, Order.CURRENCY_GBP)
        self.assertEqual(order.fulfilment_method, Order.FULFILMENT_WORKSHOP_FITTING)
        self.assertEqual(order.fitting_charge, Decimal("0.00"))
        self.assertEqual(order.tax_treatment, Order.TAX_NOT_VAT_REGISTERED)
        self.assertEqual(order.tax_amount, Decimal("0.00"))
        self.assertTrue(order.reference.startswith("PV-"))
        self.assertEqual(len(order.reference), 15)

    def test_configured_line_arithmetic_and_quantity_bounds(self):
        order, _catalogue = create_snapshot_order(quantity=2)
        item = order.items.get()
        self.assertEqual(item.base_unit_price, Decimal("695.00"))
        self.assertEqual(item.options_total, Decimal("130.00"))
        self.assertEqual(item.configured_unit_price, Decimal("825.00"))
        self.assertEqual(item.line_total, Decimal("1650.00"))
        self.assertEqual(order.full_total, Decimal("1650.00"))
        split = calculate_deposit(Decimal("1650.00"))
        self.assertEqual(order.deposit_required, split.deposit)
        self.assertEqual(order.balance_on_completion, split.balance)

        with self.assertRaises(ValidationError):
            OrderItem.objects.create(
                order=order,
                position=2,
                original_product_id=item.original_product_id,
                product_name=item.product_name,
                product_slug=item.product_slug,
                configuration_signature=item.configuration_signature,
                quantity=0,
                base_unit_price=item.base_unit_price,
                options_total=item.options_total,
                configured_unit_price=item.configured_unit_price,
                line_total=Decimal("0.00"),
            )
        with self.assertRaises(ValidationError):
            OrderItem.objects.create(
                order=order,
                position=3,
                original_product_id=item.original_product_id,
                product_name=item.product_name,
                product_slug=item.product_slug,
                configuration_signature=item.configuration_signature,
                quantity=1000,
                base_unit_price=item.base_unit_price,
                options_total=item.options_total,
                configured_unit_price=item.configured_unit_price,
                line_total=item.configured_unit_price * 1000,
            )

    def test_zero_price_option_is_stored_in_the_snapshot(self):
        _order, _catalogue = create_snapshot_order()
        none_option = OrderItemOption.objects.get(option_name="None")
        self.assertEqual(none_option.price_adjustment, Decimal("0.00"))
        self.assertEqual(none_option.group_name, "Lights")

    def test_catalogue_edits_do_not_change_snapshots(self):
        order, catalogue = create_snapshot_order()
        item = order.items.get()
        lwb = item.selected_options.get(option_name="LWB")
        original_name = item.product_name
        original_line_total = item.line_total
        original_adjustment = lwb.price_adjustment

        catalogue["product"].name = "Changed Rack"
        catalogue["product"].price = Decimal("999.00")
        catalogue["product"].save()
        catalogue["lwb"].name = "Changed LWB"
        catalogue["lwb"].price_adjustment = Decimal("500.00")
        catalogue["lwb"].save()

        item.refresh_from_db()
        lwb.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(item.product_name, original_name)
        self.assertEqual(item.line_total, original_line_total)
        self.assertEqual(lwb.option_name, "LWB")
        self.assertEqual(lwb.price_adjustment, original_adjustment)
        self.assertEqual(order.full_total, Decimal("825.00"))

    def test_catalogue_and_cart_deletion_leave_snapshots(self):
        cart = Cart.objects.create(session_key="order-source")
        order, catalogue = create_snapshot_order(source_cart=cart)
        item = order.items.get()
        CartItem.objects.create(cart=cart, product=catalogue["product"], quantity=1)
        product_id = catalogue["product"].id
        order_id = order.id
        item_id = item.id

        cart.delete()
        catalogue["product"].delete()

        order = Order.objects.get(id=order_id)
        item = OrderItem.objects.get(id=item_id)
        self.assertIsNone(order.source_cart_id)
        self.assertIsNone(item.product_id)
        self.assertEqual(item.original_product_id, product_id)
        self.assertEqual(item.product_name, "Retro Roof Rack")
        self.assertEqual(item.line_total, Decimal("825.00"))
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(item.selected_options.count(), 2)
        self.assertTrue(
            item.selected_options.filter(option_name="LWB", price_adjustment=Decimal("130.00")).exists()
        )

    def test_normal_snapshot_edits_are_rejected(self):
        order, _catalogue = create_snapshot_order()
        item = order.items.get()
        option = item.selected_options.get(option_name="LWB")
        original_reference = order.reference

        order.full_total = Decimal("100.00")
        with self.assertRaises(ValidationError):
            order.save()
        order.refresh_from_db()
        self.assertEqual(order.full_total, Decimal("825.00"))
        self.assertEqual(order.reference, original_reference)

        order.reference = "PV-TAMPERED"
        with self.assertRaises(ValidationError):
            order.save()

        item.quantity = 9
        with self.assertRaises(ValidationError):
            item.save()
        item.refresh_from_db()
        self.assertEqual(item.quantity, 1)

        option.price_adjustment = Decimal("1.00")
        with self.assertRaises(ValidationError):
            option.save()
        option.refresh_from_db()
        self.assertEqual(option.price_adjustment, Decimal("130.00"))

    def test_internal_notes_update_leaves_financial_snapshot(self):
        order, _catalogue = create_snapshot_order()
        original = {
            "reference": order.reference,
            "full_total": order.full_total,
            "deposit_required": order.deposit_required,
            "balance_on_completion": order.balance_on_completion,
            "amount_paid": order.amount_paid,
            "payment_status": order.payment_status,
        }
        order.internal_notes = "Customer asked about fitting date."
        order.save()
        order.refresh_from_db()
        self.assertEqual(order.internal_notes, "Customer asked about fitting date.")
        for field, value in original.items():
            self.assertEqual(getattr(order, field), value)

    def test_mismatched_deposit_is_rejected(self):
        split = calculate_deposit(Decimal("825.00"))
        with self.assertRaises(ValidationError):
            Order.objects.create(
                customer_name="Alex Farmer",
                customer_email="alex@example.com",
                customer_phone="01254 000000",
                vehicle_make="Volkswagen",
                vehicle_model="T6.1",
                vehicle_year=2021,
                vehicle_wheelbase="LWB",
                full_total=Decimal("825.00"),
                deposit_required=Decimal("200.00"),
                balance_on_completion=split.balance,
            )


class ExistingApiUnaffectedTests(APITestCase):
    def test_product_option_price_alias_and_cart_add_still_work(self):
        category = Category.objects.create(
            name="Racks",
            slug="racks-order-smoke",
            type="product",
        )
        product = Product.objects.create(
            category=category,
            name="Smoke Rack",
            slug="smoke-rack",
            price=Decimal("695.00"),
        )
        group = ProductOptionGroup.objects.create(
            product=product,
            name="Wheelbase",
            required=True,
        )
        option = ProductOption.objects.create(
            group=group,
            name="MWB",
            price_adjustment=Decimal("85.50"),
        )
        detail = self.client.get(reverse("product-detail-api", kwargs={"slug": product.slug}))
        payload_option = detail.data["option_groups"][0]["options"][0]
        self.assertEqual(payload_option["price"], "85.50")
        self.assertEqual(payload_option["price_adjustment"], "85.50")

        cart = Cart.objects.create(session_key="order-smoke")
        add = self.client.post(
            reverse("add_to_cart"),
            {
                "cart_id": str(cart.id),
                "product_id": product.id,
                "quantity": 1,
                "options": [option.id],
            },
            format="json",
        )
        self.assertEqual(add.status_code, 201)
        self.assertEqual(add.data["cart"]["items"][0]["configured_unit_price"], "780.50")
        self.assertFalse(CartItem.objects.filter(configuration_signature="").exists())
