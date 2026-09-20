from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from cart.models import Cart, CartItem
from store.models import Product

from .models import Order


class DepositOrderTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.product = Product.objects.create(
            name="Test product",
            slug="test-product",
            price=Decimal("100.00"),
        )
        self.cart = Cart.objects.create(session_key="deposit-test")
        CartItem.objects.create(cart=self.cart, product=self.product, quantity=1)

    def test_one_third_deposit_is_rounded_and_balance_is_remainder(self):
        self.assertEqual(Order.calculate_deposit(Decimal("100.00")), Decimal("33.33"))
        self.assertEqual(Order.calculate_deposit(Decimal("50.00")), Decimal("16.67"))

    @patch("orders.views.stripe.checkout.Session.create")
    def test_create_deposit_checkout_creates_order_and_checkout(self, create_session):
        create_session.return_value = {"id": "cs_test_123", "url": "https://checkout.stripe.test/session"}
        response = self.client.post(
            "/api/orders/deposit/checkout/",
            {
                "cart_id": str(self.cart.id),
                "customer": {
                    "name": "Test Customer",
                    "email": "test@example.com",
                    "line1": "1 Test Street",
                    "city": "Accrington",
                    "postcode": "BB5 1AA",
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(id=response.data["order_id"])
        self.assertEqual(order.total_amount, Decimal("100.00"))
        self.assertEqual(order.deposit_amount, Decimal("33.33"))
        self.assertEqual(order.balance_amount, Decimal("66.67"))
        self.assertEqual(order.status, "pending_deposit")
        create_session.assert_called_once()

    @patch("orders.views.stripe.checkout.Session.create")
    def test_deposit_webhook_marks_balance_due_and_clears_cart(self, create_session):
        create_session.return_value = {"id": "cs_test_123", "url": "https://checkout.stripe.test/session"}
        create_response = self.client.post(
            "/api/orders/deposit/checkout/",
            {
                "cart_id": str(self.cart.id),
                "customer": {"name": "Test", "email": "test@example.com"},
            },
            format="json",
        )
        order_id = create_response.data["order_id"]
        payload = {
            "id": "evt_test",
            "type": "checkout.session.completed",
            "data": {"object": {
                "metadata": {"order_id": order_id, "payment_stage": "deposit"},
                "payment_status": "paid",
                "payment_intent": "pi_test",
            }},
        }
        with patch("orders.views.stripe.Webhook.construct_event", return_value=payload):
            response = self.client.post(
                "/api/orders/stripe/webhook/",
                data="{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="test",
            )
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(id=order_id)
        self.assertEqual(order.status, "balance_due")
        self.assertEqual(order.deposit_amount, Decimal("33.33"))
        self.assertEqual(CartItem.objects.filter(cart=self.cart).count(), 0)
