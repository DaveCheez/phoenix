"""Public unpaid order creation and confirmation."""

import threading
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient, APITransactionTestCase

from cart.configuration import build_configuration_signature
from cart.guest_access import issue_guest_cart
from cart.models import Cart, CartItem, CartItemOption, GuestSession
from cart.test_application_client import (
    TEST_APP_CREDENTIAL,
    TEST_SHOPPER_ADDRESS,
    CartAPIClient,
)

from .factories import build_catalogue
from .models import Order, OrderCreationAttempt
from .money import calculate_deposit
from .review import review_token_for_cart


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
        if body is None:
            body = {
                **_CUSTOMER,
                "cart_review_token": review_token_for_cart(self.issued.cart),
            }
        return self.client.post(
            "/api/orders/",
            body,
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
        self.issued.cart.refresh_from_db()
        self.assertEqual(self.issued.cart.items.count(), 0)
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
        self.assertEqual(response.data["code"], "CONFIGURATION_CONFLICT")
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderCreationAttempt.objects.exists())


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL idempotency race only. Skipped on the normal SQLite run.",
)
@override_settings(CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL)
class PublicOrderPostgresTests(TransactionTestCase):
    def test_concurrent_same_key_creates_one_order(self):
        catalogue = build_catalogue()
        issued = _guest_cart(catalogue)
        body = {
            **_CUSTOMER,
            "cart_review_token": review_token_for_cart(issued.cart),
        }
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
                    body,
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
        issued.cart.refresh_from_db()
        self.assertEqual(issued.cart.items.count(), 0)
        self.assertIsNotNone(issued.cart.guest_session_id)

    def test_mutation_before_submit_rejects_the_stale_review(self):
        catalogue = build_catalogue()
        issued = _guest_cart(catalogue)
        token = review_token_for_cart(issued.cart)
        item = issued.cart.items.get()
        held = threading.Event()
        release = threading.Event()
        state = {}

        def mutate():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                with transaction.atomic():
                    GuestSession.objects.select_for_update().get(
                        pk=issued.cart.guest_session_id
                    )
                    Cart.objects.select_for_update().get(pk=issued.cart.id)
                    item.quantity = 3
                    item.save(update_fields=["quantity", "updated_at"])
                    held.set()
                    self.assertTrue(release.wait(20))
            except Exception as exc:
                state["mutate_error"] = exc
                held.set()
            finally:
                thread_connection.close()

        def submit():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                thread_connection.ensure_connection()
                state["submit_pid"] = thread_connection.connection.get_backend_pid()
                state["submit_ready"] = True
                response = CartAPIClient().post(
                    "/api/orders/",
                    {**_CUSTOMER, "cart_review_token": token},
                    format="json",
                    HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
                    HTTP_IDEMPOTENCY_KEY="stale-race-1",
                )
                state["status"] = response.status_code
                state["code"] = response.data.get("code")
            except Exception as exc:
                state["submit_error"] = exc
            finally:
                thread_connection.close()

        mutator = threading.Thread(target=mutate)
        mutator.start()
        submitter = threading.Thread(target=submit)
        try:
            self.assertTrue(held.wait(15))
            self.assertNotIn("mutate_error", state)
            submitter.start()
            deadline = time.monotonic() + 15
            while "submit_pid" not in state and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn("submit_pid", state)
            self.assertTrue(_wait_for_lock(state["submit_pid"]))
        finally:
            release.set()
            mutator.join(20)
            if submitter.is_alive() or "submit_pid" in state:
                submitter.join(20)
        self.assertFalse(mutator.is_alive())
        self.assertFalse(submitter.is_alive())
        self.assertNotIn("mutate_error", state)
        self.assertNotIn("submit_error", state)
        self.assertEqual(state["status"], 409)
        self.assertEqual(state["code"], "CART_CHANGED")
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderCreationAttempt.objects.exists())
        item.refresh_from_db()
        self.assertEqual(item.quantity, 3)

    def test_submit_before_add_keeps_the_later_line(self):
        catalogue = build_catalogue()
        issued = _guest_cart(catalogue)
        submitted_id = issued.cart.items.get().id
        token = review_token_for_cart(issued.cart)
        held = threading.Event()
        release = threading.Event()
        state = {}

        def pause():
            held.set()
            self.assertTrue(release.wait(20))

        def submit():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                response = CartAPIClient().post(
                    "/api/orders/",
                    {**_CUSTOMER, "cart_review_token": token},
                    format="json",
                    HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
                    HTTP_IDEMPOTENCY_KEY="clear-race-1",
                )
                state["status"] = response.status_code
                state["reference"] = response.data.get("order", {}).get("reference")
            except Exception as exc:
                state["submit_error"] = exc
            finally:
                thread_connection.close()

        def add_later():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                thread_connection.ensure_connection()
                state["add_pid"] = thread_connection.connection.get_backend_pid()
                response = CartAPIClient().post(
                    "/api/cart/add/",
                    {
                        "product_id": catalogue["product"].id,
                        "quantity": 2,
                        "options": [catalogue["no_lights"].id, catalogue["lwb"].id],
                    },
                    format="json",
                    HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
                )
                state["add_status"] = response.status_code
            except Exception as exc:
                state["add_error"] = exc
            finally:
                thread_connection.close()

        submitter = threading.Thread(target=submit)
        adder = threading.Thread(target=add_later)
        with patch("orders.access._submission_ready_to_commit", side_effect=pause):
            submitter.start()
            try:
                self.assertTrue(held.wait(15))
                adder.start()
                deadline = time.monotonic() + 15
                while "add_pid" not in state and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIn("add_pid", state)
                self.assertTrue(_wait_for_lock(state["add_pid"]))
            finally:
                release.set()
                submitter.join(20)
                adder.join(20)
        self.assertNotIn("submit_error", state)
        self.assertNotIn("add_error", state)
        self.assertEqual(state["status"], 201)
        self.assertEqual(state["add_status"], 201)
        self.assertEqual(Order.objects.count(), 1)
        order = Order.objects.get()
        self.assertEqual(order.reference, state["reference"])
        self.assertEqual(order.items.get().quantity, 1)
        remaining = CartItem.objects.get(cart=issued.cart)
        self.assertNotEqual(remaining.id, submitted_id)
        self.assertEqual(remaining.quantity, 2)
        self.assertTrue(Cart.objects.filter(pk=issued.cart.id).exists())


def _wait_for_lock(pid):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                [pid],
            )
            row = cursor.fetchone()
        if row and row[0] == "Lock":
            return True
        time.sleep(0.05)
    return False
