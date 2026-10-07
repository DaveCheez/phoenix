"""HTTP enforcement over the shared cart counters. Default stays disabled."""

import threading
import unittest
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db import IntegrityError, connection
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITransactionTestCase
from unittest.mock import patch

from store.models import Category, Product

from .guest_access import issue_guest_cart
from .models import Cart, CartItem, CartRateLimitCounter, GuestSession
from .rate_limit import RateLimitStoreError
from .test_application_client import (
    TEST_APP_CREDENTIAL,
    TEST_SHOPPER_ADDRESS,
    CartAPIClient,
)
from .test_rate_limit import TEST_HMAC_KEY


def _policies(**limits):
    def entries(limit):
        return [{"window_seconds": 60, "limit": limit}]

    return {
        "issuance": entries(limits.get("issuance", 20)),
        "failed_access": entries(limits.get("failed_access", 20)),
        "authenticated_cart": entries(limits.get("authenticated_cart", 20)),
    }


def _enabled(**limits):
    return override_settings(
        CART_RATE_LIMIT_ENABLED=True,
        CART_RATE_LIMIT_POLICIES=_policies(**limits),
        CART_RATE_LIMIT_HMAC_KEY=TEST_HMAC_KEY,
        CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
    )


def _product():
    category = Category.objects.create(name="Limit rack", slug="limit-rack", type="product")
    return Product.objects.create(
        category=category,
        name="Limit clip",
        slug="limit-clip",
        price=Decimal("10.00"),
    )


def _counts():
    return {
        row.scope: row.count
        for row in CartRateLimitCounter.objects.all()
    }


@override_settings(
    CART_RATE_LIMIT_ENABLED=False,
    CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
)
class RateLimitDisabledTests(APITransactionTestCase):
    def test_disabled_enforcement_does_not_call_accounting(self):
        with patch("cart.rate_limit_http.consume_windows", side_effect=AssertionError("called")):
            response = CartAPIClient().post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )
        self.assertEqual(response.status_code, 201)
        self.assertFalse(CartRateLimitCounter.objects.exists())


@_enabled()
class RateLimitHttpTests(APITransactionTestCase):
    def test_invalid_enabled_configuration_fails_without_writes(self):
        cases = [
            {"CART_RATE_LIMIT_ENABLED": "false", "CART_RATE_LIMIT_POLICIES": _policies()},
            {"CART_RATE_LIMIT_POLICIES": {}},
            {"CART_RATE_LIMIT_POLICIES": {"issuance": [{"window_seconds": 60, "limit": 1}]}},
            {"CART_RATE_LIMIT_HMAC_KEY": ""},
        ]
        for extra in cases:
            with override_settings(**extra):
                with patch(
                    "cart.rate_limit_http.consume_windows",
                    side_effect=AssertionError("called"),
                ):
                    response = CartAPIClient().post(
                        reverse("create_cart"),
                        {"action": "start"},
                        format="json",
                    )
            self.assertEqual(response.status_code, 503, extra)
            self.assertEqual(response.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
            self.assertNotIn("WWW-Authenticate", response)
        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(CartRateLimitCounter.objects.exists())

    def test_application_rejection_creates_no_counter(self):
        response = APIClient().post(
            reverse("create_cart"),
            {"action": "start"},
            format="json",
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "CART_APPLICATION_REJECTED")
        self.assertFalse(CartRateLimitCounter.objects.exists())
        self.assertFalse(GuestSession.objects.exists())

    def test_explicit_start_consumes_issuance_only(self):
        response = CartAPIClient().post(
            reverse("create_cart"),
            {"action": "start"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(_counts(), {"issuance": 1})
        self.assertEqual(GuestSession.objects.count(), 1)

    def test_one_address_shares_issuance_and_other_addresses_do_not(self):
        with override_settings(CART_RATE_LIMIT_POLICIES=_policies(issuance=1)):
            first = CartAPIClient().post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )
            second = CartAPIClient().post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )
            other = CartAPIClient().post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
                HTTP_X_PHOENIX_SHOPPER_ADDRESS="198.51.100.20",
            )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 429)
        self.assertGreaterEqual(int(second["Retry-After"]), 1)
        self.assertLessEqual(int(second["Retry-After"]), 60)
        self.assertEqual(second["Cache-Control"], "no-store")
        self.assertEqual(second.data["code"], "CART_RATE_LIMITED")
        self.assertNotIn("WWW-Authenticate", second)
        self.assertNotIn("guest_access", second.data)
        self.assertEqual(other.status_code, 201)
        self.assertEqual(GuestSession.objects.count(), 2)
        self.assertEqual(CartRateLimitCounter.objects.filter(scope="issuance").count(), 2)

    def test_start_required_and_legacy_cart_id_do_not_issue(self):
        missing = CartAPIClient().post(reverse("create_cart"), {}, format="json")
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.data["code"], "START_REQUIRED")
        legacy = Cart.objects.create()
        claimed = CartAPIClient().post(
            reverse("create_cart"),
            {"action": "start", "cart_id": str(legacy.id)},
            format="json",
        )
        self.assertEqual(claimed.status_code, 401)
        self.assertEqual(_counts(), {"failed_access": 1})
        self.assertFalse(GuestSession.objects.exists())
        legacy.refresh_from_db()
        self.assertIsNone(legacy.guest_session_id)

    def test_invalid_guest_uses_failed_access_and_does_not_block_a_valid_guest(self):
        issued = issue_guest_cart()
        with override_settings(CART_RATE_LIMIT_POLICIES=_policies(failed_access=1)):
            denied = CartAPIClient().get(
                reverse("get_cart"),
                HTTP_AUTHORIZATION="Bearer not-a-token",
            )
            blocked = CartAPIClient().get(
                reverse("get_cart"),
                HTTP_AUTHORIZATION="Bearer not-a-token",
            )
            allowed = CartAPIClient().get(
                reverse("get_cart"),
                HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
            )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(CartRateLimitCounter.objects.filter(scope="failed_access").get().count, 2)
        self.assertEqual(CartRateLimitCounter.objects.filter(scope="authenticated_cart").get().count, 1)

    def test_separate_sessions_have_separate_authenticated_budgets(self):
        first = issue_guest_cart()
        second = issue_guest_cart()
        product = _product()
        with override_settings(CART_RATE_LIMIT_POLICIES=_policies(authenticated_cart=1)):
            added = self._auth_post(first, reverse("add_to_cart"), {"product_id": product.id, "quantity": 1, "options": []})
            again = self._auth_post(first, reverse("add_to_cart"), {"product_id": product.id, "quantity": 1, "options": []})
            other = self._auth_post(second, reverse("add_to_cart"), {"product_id": product.id, "quantity": 1, "options": []})
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.data["cart"]["items"][0]["configured_unit_price"], "10.00")
        self.assertEqual(again.status_code, 429)
        self.assertEqual(other.status_code, 201)
        self.assertEqual(CartItem.objects.filter(cart=first.cart).get().quantity, 1)
        self.assertEqual(CartRateLimitCounter.objects.filter(scope="authenticated_cart").count(), 2)

    def test_authenticated_routes_count_once_each(self):
        issued = issue_guest_cart()
        product = _product()
        client = CartAPIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
            HTTP_X_PHOENIX_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
            HTTP_X_PHOENIX_SHOPPER_ADDRESS=TEST_SHOPPER_ADDRESS,
        )
        added = client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 1, "options": []},
            format="json",
        )
        item_id = added.data["cart"]["items"][0]["item_id"]
        calls = [
            client.get(reverse("get_cart")),
            client.post(reverse("update_cart_item"), {"item_id": item_id, "quantity": 2}, format="json"),
            client.patch(reverse("update_cart_item"), {"item_id": item_id, "quantity": 1}, format="json"),
            client.post(reverse("create_cart"), {"action": "start"}, format="json"),
            client.post(reverse("create_cart"), {"action": "replace_missing"}, format="json"),
            client.post(reverse("remove_from_cart"), {"item_id": item_id}, format="json"),
            client.post(reverse("add_to_cart"), {"product_id": product.id, "quantity": 1, "options": []}, format="json"),
        ]
        item_id = CartItem.objects.get().id
        calls.append(client.delete(reverse("remove_from_cart"), {"item_id": item_id}, format="json"))
        calls.append(client.post(reverse("add_to_cart"), {"product_id": product.id, "quantity": 1, "options": []}, format="json"))
        calls.append(client.post(reverse("clear_cart"), {}, format="json"))
        calls.append(client.post(reverse("add_to_cart"), {"product_id": product.id, "quantity": 1, "options": []}, format="json"))
        calls.append(client.delete(reverse("clear_cart"), {}, format="json"))
        self.assertTrue(all(response.status_code < 300 for response in calls))
        self.assertEqual(_counts(), {"authenticated_cart": 1 + len(calls)})
        self.assertTrue(GuestSession.objects.filter(pk=issued.cart.guest_session_id).exists())
        self.assertTrue(Cart.objects.filter(pk=issued.cart.id).exists())

    def test_missing_cart_and_mismatched_id_stay_authenticated(self):
        issued = issue_guest_cart()
        session_id = issued.cart.guest_session_id
        issued.cart.delete()
        missing = CartAPIClient().get(
            reverse("get_cart"),
            HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
        )
        self.assertEqual(missing.status_code, 409)
        replaced = CartAPIClient().post(
            reverse("create_cart"),
            {"action": "replace_missing"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
        )
        self.assertEqual(replaced.status_code, 201)
        mismatched = CartAPIClient().get(
            reverse("get_cart") + "?cart_id=00000000-0000-0000-0000-000000000099",
            HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
        )
        self.assertEqual(mismatched.status_code, 401)
        self.assertEqual(_counts(), {"authenticated_cart": 3})
        self.assertEqual(GuestSession.objects.get().id, session_id)

    def test_validation_error_still_consumes_the_authenticated_attempt(self):
        issued = issue_guest_cart()
        response = CartAPIClient().post(
            reverse("add_to_cart"),
            {"product_id": _product().id, "quantity": "many", "options": []},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(_counts(), {"authenticated_cart": 1})
        self.assertFalse(CartItem.objects.exists())

    def test_issuance_failure_after_accounting_keeps_the_attempt(self):
        client = CartAPIClient()
        client.raise_request_exception = False
        with patch("cart.views.issue_guest_cart", side_effect=RuntimeError("rate report")):
            response = client.post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(b"guest_access", response.content)
        self.assertEqual(_counts(), {"issuance": 1})
        self.assertFalse(GuestSession.objects.exists())

    def test_database_outage_during_issuance_keeps_the_attempt_and_creates_nothing(self):
        from django.db import OperationalError

        with patch("cart.views.issue_guest_cart", side_effect=OperationalError("down")):
            response = CartAPIClient().post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
        self.assertEqual(_counts(), {"issuance": 1})
        self.assertFalse(GuestSession.objects.exists())

    def test_guest_revoked_after_accounting_is_rejected_before_the_write(self):
        issued = issue_guest_cart()
        product = _product()
        real = __import__("cart.http_access", fromlist=["get_guest_session"]).get_guest_session

        def revoke_then_resolve(**kwargs):
            GuestSession.objects.filter(pk=issued.cart.guest_session_id).update(
                revoked_at=timezone.now()
            )
            return real(**kwargs)

        with patch("cart.http_access.get_guest_session", side_effect=revoke_then_resolve):
            response = CartAPIClient().post(
                reverse("add_to_cart"),
                {"product_id": product.id, "quantity": 1, "options": []},
                format="json",
                HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
            )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(CartItem.objects.exists())
        self.assertEqual(_counts(), {"authenticated_cart": 1})

    def test_store_outage_follows_the_documented_scope_policy(self):
        issued = issue_guest_cart()
        with patch("cart.rate_limit_http.consume_windows", side_effect=RateLimitStoreError("down")):
            started = CartAPIClient().post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )
            failed = CartAPIClient().get(reverse("get_cart"), HTTP_AUTHORIZATION="Bearer not-a-token")
            continued = CartAPIClient().get(
                reverse("get_cart"),
                HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
            )
        self.assertEqual(started.status_code, 503)
        self.assertEqual(started.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
        self.assertEqual(failed.status_code, 401)
        self.assertEqual(failed.data["code"], "CART_ACCESS_UNAVAILABLE")
        self.assertEqual(continued.status_code, 200)
        self.assertFalse(GuestSession.objects.exclude(pk=issued.cart.guest_session_id).exists())

    def test_programming_and_integrity_errors_do_not_fail_open(self):
        self.client = CartAPIClient()
        self.client.raise_request_exception = False
        for error in (RuntimeError("rate report"), IntegrityError("constraint")):
            with patch("cart.rate_limit_http.consume_windows", side_effect=error):
                response = self.client.post(
                    reverse("create_cart"),
                    {"action": "start"},
                    format="json",
                )
            self.assertEqual(response.status_code, 500, type(error))
            self.assertNotIn(b"guest_access", response.content)
        self.assertFalse(GuestSession.objects.exists())

    def test_fixed_window_boundary_starts_a_new_issuance_budget(self):
        opening = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        with override_settings(CART_RATE_LIMIT_POLICIES=_policies(issuance=1)):
            with patch("cart.rate_limit.accounting_now", return_value=opening):
                first = CartAPIClient().post(
                    reverse("create_cart"),
                    {"action": "start"},
                    format="json",
                )
                denied = CartAPIClient().post(
                    reverse("create_cart"),
                    {"action": "start"},
                    format="json",
                )
            with patch(
                "cart.rate_limit.accounting_now",
                return_value=opening + timedelta(seconds=60),
            ):
                nxt = CartAPIClient().post(
                    reverse("create_cart"),
                    {"action": "start"},
                    format="json",
                )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(denied.status_code, 429)
        self.assertEqual(nxt.status_code, 201)
        self.assertEqual(GuestSession.objects.count(), 2)

    def test_options_and_unsupported_methods_do_not_count(self):
        options = CartAPIClient().options(reverse("create_cart"))
        rejected = CartAPIClient().put(reverse("add_to_cart"), {}, format="json")
        self.assertEqual(options.status_code, 200)
        self.assertEqual(rejected.status_code, 405)
        self.assertFalse(CartRateLimitCounter.objects.exists())

    def _auth_post(self, issued, url, payload):
        return CartAPIClient().post(
            url,
            payload,
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
        )


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL HTTP counter concurrency only. Skipped on the normal SQLite run.",
)
@_enabled(issuance=3, authenticated_cart=5)
class RateLimitHttpPostgresTests(TransactionTestCase):
    def test_exactly_n_issuance_requests_are_admitted(self):
        barrier = threading.Barrier(6)
        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            from django.db import connection as thread_connection

            thread_connection.close()
            client = CartAPIClient()
            try:
                barrier.wait(15)
                response = client.post(
                    reverse("create_cart"),
                    {"action": "start"},
                    format="json",
                )
                with lock:
                    results.append(response.status_code)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                thread_connection.close()

        workers = [threading.Thread(target=worker) for _ in range(6)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [201, 201, 201, 429, 429, 429])
        self.assertEqual(GuestSession.objects.count(), 3)
        self.assertEqual(Cart.objects.count(), 3)
        self.assertEqual(CartRateLimitCounter.objects.get(scope="issuance").count, 6)

    def test_authenticated_limit_commits_before_the_cart_write(self):
        issued = issue_guest_cart()
        product = _product()
        seen = {}

        def pausing(**kwargs):
            def read_counter():
                from django.db import connection as thread_connection

                thread_connection.close()
                try:
                    seen["count"] = CartRateLimitCounter.objects.get(
                        scope="authenticated_cart"
                    ).count
                finally:
                    thread_connection.close()

            reader = threading.Thread(target=read_counter)
            reader.start()
            reader.join(10)
            from cart.operations import add_product_to_cart as real_add

            return real_add(**kwargs)

        with patch("cart.views.add_product_to_cart", side_effect=pausing):
            response = CartAPIClient().post(
                reverse("add_to_cart"),
                {"product_id": product.id, "quantity": 1, "options": []},
                format="json",
                HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(seen.get("count"), 1)
        self.assertEqual(CartItem.objects.count(), 1)
