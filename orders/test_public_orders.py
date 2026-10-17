"""Public unpaid order creation and confirmation."""

import threading
import unittest
from decimal import Decimal

from django.db import connection
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient, APITransactionTestCase

from cart.configuration import build_configuration_signature
from cart.guest_access import issue_guest_cart
from cart.models import CartItem, CartItemOption
from cart.test_application_client import (
    TEST_APP_CREDENTIAL,
    TEST_SHOPPER_ADDRESS,
    CartAPIClient,
)

from .factories import build_catalogue
from .models import Order, OrderCreationAttempt
from .money import calculate_deposit


_CUSTOMER = {
    "customer_name": "Alex Farmer",
    "customer_email": "alex@example.com",
    "customer_phone": "01254 000000",
    "vehicle_make": "Volkswagen",
    "vehicle_model": "T6.1",
    "vehicle_year": 2021,
    "vehicle_wheelbase": "LWB",
}


def _line(cart, catalogue, *, quantity=1, signature=None):
    product = catalogue["product"]
    options = [catalogue["no_lights"], catalogue["lwb"]]
    if signature is None:
        signature = build_configuration_signature([option.id for option in options])
    item = CartItem.objects.create(
        cart=cart,
        product=product,
        quantity=quantity,
        configuration_signature=signature,
    )
    for option in options:
        CartItemOption.objects.create(cart_item=item, group=option.group, option=option)
    return item


def _guest_cart(catalogue):
    issued = issue_guest_cart()
    _line(issued.cart, catalogue)
    return issued


@override_settings(CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL)
class PublicOrderTests(APITransactionTestCase):
    def setUp(self):
        self.catalogue = build_catalogue()
        self.issued = _guest_cart(self.catalogue)
        self.client = CartAPIClient()

    def _post(self, token, body=None, key="idem-key-1", **extra):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}", "HTTP_IDEMPOTENCY_KEY": key}
        headers.update(extra)
        return self.client.post(
            "/api/orders/",
            _CUSTOMER if body is None else body,
            format="json",
            **headers,
        )

    def test_guest_creates_one_unpaid_order_from_the_server_snapshot(self):
        response = self._post(self.issued.raw_token, {**_CUSTOMER, "full_total": "1.00"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Order.objects.exists())

        response = self._post(self.issued.raw_token)
        self.assertEqual(response.status_code, 201, response.content)
        order = Order.objects.get()
        split = calculate_deposit(Decimal("825.00"))
        self.assertEqual(order.full_total, Decimal("825.00"))
        self.assertEqual(order.deposit_required, split.deposit)
        self.assertEqual(order.balance_on_completion, split.balance)
        self.assertEqual(order.amount_paid, Decimal("0.00"))
        self.assertEqual(order.payment_status, "pending_deposit")
        self.assertEqual(order.fitting_charge, Decimal("0.00"))
        self.assertEqual(order.tax_amount, Decimal("0.00"))
        self.assertEqual(order.guest_session_id, self.issued.cart.guest_session_id)
        self.assertEqual(self.issued.cart.items.count(), 1)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.data["order"]["reference"], order.reference)
        self.assertNotIn("internal_notes", response.data["order"])
        self.assertNotIn("id", response.data["order"])
        self.assertNotIn(self.issued.raw_token, response.content.decode())

    def test_missing_or_other_guest_cannot_use_this_basket(self):
        missing = APIClient().post(
            "/api/orders/",
            _CUSTOMER,
            format="json",
            HTTP_X_PHOENIX_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
            HTTP_X_PHOENIX_SHOPPER_ADDRESS=TEST_SHOPPER_ADDRESS,
            HTTP_IDEMPOTENCY_KEY="idem-key-2",
        )
        self.assertEqual(missing.status_code, 401)
        other = _guest_cart(self.catalogue)
        other.cart.items.all().delete()
        stolen = self._post(other.raw_token, key="idem-key-3")
        self.assertEqual(stolen.status_code, 400)
        self.assertEqual(stolen.data["code"], "EMPTY_CART")
        self.assertFalse(Order.objects.exists())

    def test_confirmation_requires_the_creating_guest(self):
        created = self._post(self.issued.raw_token)
        reference = created.data["order"]["reference"]
        bare = self.client.get(f"/api/orders/{reference}/")
        self.assertEqual(bare.status_code, 401)
        other = issue_guest_cart()
        denied = self.client.get(
            f"/api/orders/{reference}/",
            HTTP_AUTHORIZATION=f"Bearer {other.raw_token}",
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.data["code"], "ORDER_NOT_AVAILABLE")
        self.assertNotIn(reference, denied.content.decode())
        allowed = self.client.get(
            f"/api/orders/{reference}/",
            HTTP_AUTHORIZATION=f"Bearer {self.issued.raw_token}",
        )
        self.assertEqual(allowed.status_code, 200)
        item = allowed.data["order"]["items"][0]
        self.assertEqual(item["product_name"], "Retro Roof Rack")
        self.assertEqual(item["configured_unit_price"], "825.00")
        self.assertEqual(
            {option["option_name"] for option in item["options"]},
            {"None", "LWB"},
        )
        self.assertNotIn("internal_notes", allowed.data["order"])
        self.assertNotIn("original_product_id", item)
        self.assertEqual(allowed["Cache-Control"], "no-store")

    def test_same_key_replays_and_a_changed_payload_conflicts(self):
        first = self._post(self.issued.raw_token, key="same-key-1")
        second = self._post(self.issued.raw_token, key="same-key-1")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["order"]["reference"], first.data["order"]["reference"])
        self.assertEqual(Order.objects.count(), 1)
        changed = self._post(
            self.issued.raw_token,
            {**_CUSTOMER, "customer_name": "Sam Farmer"},
            key="same-key-1",
        )
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.data["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderCreationAttempt.objects.count(), 1)

    def test_stale_cart_creates_no_order(self):
        item = self.issued.cart.items.get()
        item.configuration_signature = "stale"
        item.save(update_fields=["configuration_signature"])
        response = self._post(self.issued.raw_token, key="stale-key-1")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.exists())


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL idempotency race only. Skipped on the normal SQLite run.",
)
@override_settings(CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL)
class PublicOrderPostgresTests(TransactionTestCase):
    def test_concurrent_same_key_creates_one_order(self):
        catalogue = build_catalogue()
        issued = _guest_cart(catalogue)
        barrier = threading.Barrier(2)
        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                barrier.wait(15)
                response = CartAPIClient().post(
                    "/api/orders/",
                    _CUSTOMER,
                    format="json",
                    HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
                    HTTP_IDEMPOTENCY_KEY="race-key-1",
                )
                with lock:
                    results.append(response.status_code)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                thread_connection.close()

        workers = [threading.Thread(target=worker) for _ in range(2)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [200, 201])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(issued.cart.items.count(), 1)
