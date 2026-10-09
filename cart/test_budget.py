"""Application-only CSRF and reset budgets. Enforcement stays disabled by default."""

import hashlib
import hmac
import sys
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import IntegrityError, ProgrammingError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.views.debug import get_exception_reporter_class
from rest_framework.test import APIClient, APITransactionTestCase
from unittest.mock import patch

from .budget import account_budget
from .guest_access import issue_guest_cart
from .models import Cart, CartRateLimitCounter, GuestSession
from .rate_limit import RateLimitNestedTransactionError, RateLimitStoreError
from .rate_limit_subjects import (
    SubjectKeyError,
    subject_key_for_address,
    subject_key_for_guest_session,
)
from .test_application_client import (
    TEST_APP_CREDENTIAL,
    TEST_SHOPPER_ADDRESS,
    CartAPIClient,
)
from .test_rate_limit import TEST_HMAC_KEY


_DOMAIN = b"phoenix-vanz.cart-rate-limit.v1"
_OTHER_ADDRESS = "198.51.100.20"


def _entries(limit):
    return [{"window_seconds": 60, "limit": limit}]


def _cart_policies():
    return {
        "issuance": _entries(20),
        "failed_access": _entries(20),
        "authenticated_cart": _entries(20),
    }


def _frontend(**limits):
    return {
        "csrf": _entries(limits.get("csrf", 5)),
        "reset": _entries(limits.get("reset", 5)),
    }


def _enabled(**limits):
    return override_settings(
        CART_RATE_LIMIT_ENABLED=True,
        CART_RATE_LIMIT_POLICIES=_cart_policies(),
        CART_FRONTEND_RATE_LIMIT_POLICIES=_frontend(**limits),
        CART_RATE_LIMIT_HMAC_KEY=TEST_HMAC_KEY,
        CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
    )


def _manual_digest(scope, kind, subject):
    message = b"\0".join(
        (
            _DOMAIN,
            scope.encode("ascii"),
            kind.encode("ascii"),
            subject.encode("ascii"),
        )
    )
    return hmac.new(bytes.fromhex(TEST_HMAC_KEY), message, hashlib.sha256).hexdigest()


@contextmanager
def _frozen_clock():
    moment = datetime(2026, 6, 1, 0, 0, 8, 800000, tzinfo=dt_timezone.utc)
    with patch("cart.rate_limit.accounting_now", return_value=moment):
        yield moment


def _budget(client, operation="csrf", **extra):
    return client.post(
        reverse("cart_budget"),
        {"operation": operation},
        format="json",
        **extra,
    )


def _sql(captured):
    return " ".join(query["sql"] for query in captured.captured_queries).lower()


def _assert_no_guest_or_cart_sql(test, captured):
    sql = _sql(captured)
    test.assertNotIn("cart_guestsession", sql)
    test.assertNotIn("cart_cartitem", sql)
    test.assertNotIn("from cart_cart", sql)
    test.assertNotIn("into cart_cart", sql)
    test.assertNotIn("update cart_cart", sql)


@override_settings(
    CART_RATE_LIMIT_ENABLED=False,
    CART_RATE_LIMIT_HMAC_KEY="",
    CART_RATE_LIMIT_POLICIES="",
    CART_FRONTEND_RATE_LIMIT_POLICIES="",
    CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
)
class BudgetDisabledTests(APITransactionTestCase):
    def test_disabled_request_is_allowed_without_accounting(self):
        with patch("cart.budget.consume_windows", side_effect=AssertionError("called")):
            with CaptureQueriesContext(connection) as captured:
                response = _budget(CartAPIClient())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "CART_BUDGET_ALLOWED")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn("WWW-Authenticate", response)
        self.assertNotIn("cart_cartratelimitcounter", _sql(captured))
        _assert_no_guest_or_cart_sql(self, captured)
        self.assertFalse(CartRateLimitCounter.objects.exists())
        self.assertFalse(GuestSession.objects.exists())

    def test_bad_frontend_policy_is_ignored_while_disabled(self):
        with override_settings(CART_FRONTEND_RATE_LIMIT_POLICIES="not-json"):
            with patch("cart.budget.consume_windows", side_effect=AssertionError("called")):
                response = _budget(CartAPIClient(), operation="reset")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "CART_BUDGET_ALLOWED")
        self.assertFalse(CartRateLimitCounter.objects.exists())


@_enabled()
class BudgetHttpTests(APITransactionTestCase):
    def test_missing_or_spoofed_application_identity_writes_nothing(self):
        cases = [
            {},
            {"HTTP_X_PHOENIX_APP_CREDENTIAL": "f" * 64},
            {"HTTP_X_PHOENIX_APP_CREDENTIAL": TEST_APP_CREDENTIAL},
            {
                "HTTP_X_PHOENIX_APP_CREDENTIAL": TEST_APP_CREDENTIAL,
                "HTTP_X_FORWARDED_FOR": _OTHER_ADDRESS,
                "REMOTE_ADDR": "192.0.2.10",
            },
            {
                "HTTP_X_PHOENIX_APP_CREDENTIAL": TEST_APP_CREDENTIAL,
                "HTTP_X_PHOENIX_SHOPPER_ADDRESS": "not-an-address",
            },
        ]
        for extra in cases:
            response = APIClient().post(
                reverse("cart_budget"),
                {"operation": "csrf"},
                format="json",
                **extra,
            )
            self.assertEqual(response.status_code, 503, extra)
            self.assertEqual(response.data["code"], "CART_APPLICATION_REJECTED")
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertNotIn("WWW-Authenticate", response)
        self.assertFalse(CartRateLimitCounter.objects.exists())
        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(Cart.objects.exists())
        malformed = APIClient().generic(
            "POST",
            reverse("cart_budget"),
            data=b"{",
            content_type="application/json",
        )
        self.assertEqual(malformed.status_code, 503)
        self.assertEqual(malformed.data["code"], "CART_APPLICATION_REJECTED")
        self.assertFalse(CartRateLimitCounter.objects.exists())

    def test_application_authentication_permits_a_request_without_a_guest(self):
        response = _budget(CartAPIClient())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"success": True, "code": "CART_BUDGET_ALLOWED"})
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(Cart.objects.exists())
        self.assertEqual(CartRateLimitCounter.objects.get().scope, "csrf")

    def test_bad_guest_bearer_is_not_a_customer_authentication_failure(self):
        with CaptureQueriesContext(connection) as captured:
            response = _budget(
                CartAPIClient(),
                HTTP_AUTHORIZATION="Bearer not-a-guest-token",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "CART_BUDGET_ALLOWED")
        self.assertNotIn("WWW-Authenticate", response)
        self.assertNotIn("not-a-guest-token", str(response.data))
        _assert_no_guest_or_cart_sql(self, captured)
        self.assertEqual(
            list(CartRateLimitCounter.objects.values_list("scope", flat=True)),
            ["csrf"],
        )

    def test_invalid_bodies_write_nothing_and_do_not_echo_values(self):
        smuggled = "smuggled-operation-value"
        raw_bodies = (
            b"",
            b"{",
            b"[]",
            b"null",
            b'"csrf"',
            b"1",
            b'{"operation": "csrf", "limit": 1}',
        )
        for body in raw_bodies:
            response = CartAPIClient().generic(
                "POST",
                reverse("cart_budget"),
                data=body,
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 400, body)
            self.assertEqual(response.data["code"], "CART_BUDGET_REQUEST_INVALID")
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertNotIn(smuggled, response.content.decode())
        objects = (
            {},
            {"operation": "CSRF"},
            {"operation": " csrf"},
            {"operation": smuggled},
            {"operation": "csrf", "limit": 1},
            {"operation": "csrf", "subject_key": "ab" * 32},
            {"operation": "csrf", "address": TEST_SHOPPER_ADDRESS},
            {"operation": "csrf", "enabled": True},
            {"operation": "reset", "cart_id": "abc", "window_seconds": 60},
        )
        for payload in objects:
            response = CartAPIClient().post(
                reverse("cart_budget"),
                payload,
                format="json",
            )
            self.assertEqual(response.status_code, 400, payload)
            self.assertEqual(response.data["code"], "CART_BUDGET_REQUEST_INVALID")
            rendered = response.content.decode()
            self.assertNotIn(smuggled, rendered)
            self.assertNotIn(TEST_SHOPPER_ADDRESS, rendered)
            self.assertNotIn(TEST_APP_CREDENTIAL, rendered)
        self.assertFalse(CartRateLimitCounter.objects.exists())

    def test_unsupported_media_and_methods_write_nothing(self):
        client = CartAPIClient()
        unsupported = client.generic(
            "POST",
            reverse("cart_budget"),
            data=b"operation=csrf",
            content_type="text/plain",
        )
        self.assertEqual(unsupported.status_code, 415)
        self.assertEqual(unsupported["Cache-Control"], "no-store")
        options = client.options(reverse("cart_budget"))
        self.assertEqual(options.status_code, 200)
        self.assertEqual(options["Cache-Control"], "no-store")
        for method in ("get", "put", "patch", "delete"):
            response = getattr(client, method)(reverse("cart_budget"))
            self.assertEqual(response.status_code, 405, method)
            self.assertEqual(response["Cache-Control"], "no-store")
        self.assertFalse(CartRateLimitCounter.objects.exists())
        self.assertFalse(GuestSession.objects.exists())

    def test_invalid_enabled_configuration_fails_closed(self):
        cases = [
            {"CART_RATE_LIMIT_ENABLED": "false"},
            {"CART_FRONTEND_RATE_LIMIT_POLICIES": ""},
            {"CART_FRONTEND_RATE_LIMIT_POLICIES": "not-json"},
            {"CART_FRONTEND_RATE_LIMIT_POLICIES": {"csrf": _entries(1)}},
            {
                "CART_FRONTEND_RATE_LIMIT_POLICIES": {
                    "csrf": _entries(1),
                    "reset": _entries(1),
                    "issuance": _entries(1),
                }
            },
            {"CART_FRONTEND_RATE_LIMIT_POLICIES": {"csrf": [], "reset": _entries(1)}},
            {"CART_RATE_LIMIT_HMAC_KEY": ""},
        ]
        for extra in cases:
            with override_settings(**extra):
                with patch(
                    "cart.budget.consume_windows",
                    side_effect=AssertionError("called"),
                ):
                    response = _budget(CartAPIClient())
            self.assertEqual(response.status_code, 503, extra)
            self.assertEqual(response.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
            self.assertNotIn(TEST_SHOPPER_ADDRESS, response.content.decode())
        with override_settings(
            CART_FRONTEND_RATE_LIMIT_POLICIES={
                "csrf": [{"window_seconds": True, "limit": 1}],
                "reset": _entries(1),
            }
        ):
            response = _budget(CartAPIClient())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
        self.assertFalse(CartRateLimitCounter.objects.exists())
        self.assertFalse(GuestSession.objects.exists())

    def test_cart_routes_keep_three_scope_policies(self):
        with _frozen_clock():
            with override_settings(CART_FRONTEND_RATE_LIMIT_POLICIES=""):
                created = CartAPIClient().post(
                    reverse("create_cart"),
                    {"action": "start"},
                    format="json",
                )
            self.assertEqual(created.status_code, 201)
            with override_settings(CART_FRONTEND_RATE_LIMIT_POLICIES="not-json"):
                again = CartAPIClient().post(
                    reverse("create_cart"),
                    {"action": "start"},
                    format="json",
                )
                budget = _budget(CartAPIClient())
        self.assertEqual(again.status_code, 201)
        self.assertEqual(budget.status_code, 503)
        self.assertEqual(budget.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
        self.assertEqual(
            list(CartRateLimitCounter.objects.values_list("scope", flat=True)),
            ["issuance"],
        )
        self.assertEqual(CartRateLimitCounter.objects.get().count, 2)

    def test_csrf_and_reset_use_separate_address_counters(self):
        with _frozen_clock(), override_settings(
            CART_FRONTEND_RATE_LIMIT_POLICIES=_frontend(csrf=1, reset=1)
        ):
            first = _budget(CartAPIClient(), operation="csrf")
            reset = _budget(CartAPIClient(), operation="reset")
            denied = _budget(CartAPIClient(), operation="csrf")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(denied.status_code, 429)
        counts = {
            row.scope: row.count
            for row in CartRateLimitCounter.objects.all()
        }
        self.assertEqual(counts, {"csrf": 2, "reset": 1})

    def test_addresses_stay_independent_of_each_other_and_of_guest_sessions(self):
        with _frozen_clock(), override_settings(
            CART_FRONTEND_RATE_LIMIT_POLICIES=_frontend(csrf=1, reset=1)
        ):
            first = _budget(CartAPIClient())
            issue_guest_cart()
            denied = _budget(CartAPIClient())
            other = _budget(
                CartAPIClient(),
                HTTP_X_PHOENIX_SHOPPER_ADDRESS=_OTHER_ADDRESS,
            )
            mapped = _budget(
                CartAPIClient(),
                HTTP_X_PHOENIX_SHOPPER_ADDRESS="::ffff:" + TEST_SHOPPER_ADDRESS,
            )
            forwarded = _budget(
                CartAPIClient(),
                HTTP_X_FORWARDED_FOR=_OTHER_ADDRESS,
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(denied.status_code, 429)
        self.assertEqual(other.status_code, 200)
        self.assertEqual(mapped.status_code, 429)
        self.assertEqual(forwarded.status_code, 429)
        self.assertEqual(GuestSession.objects.count(), 1)
        csrf_rows = CartRateLimitCounter.objects.filter(scope="csrf")
        self.assertEqual(csrf_rows.count(), 2)
        self.assertEqual(sum(row.count for row in csrf_rows), 5)

    def test_denial_commits_the_attempt_and_does_not_write_a_guest(self):
        with _frozen_clock(), override_settings(
            CART_FRONTEND_RATE_LIMIT_POLICIES=_frontend(reset=1)
        ):
            allowed = _budget(CartAPIClient(), operation="reset")
            denied = _budget(CartAPIClient(), operation="reset")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 429)
        self.assertEqual(denied.data["code"], "CART_RATE_LIMITED")
        self.assertEqual(denied.data["error"], "Cart access is temporarily limited.")
        self.assertEqual(denied["Cache-Control"], "no-store")
        self.assertEqual(int(denied["Retry-After"]), 52)
        self.assertNotIn("WWW-Authenticate", denied)
        self.assertNotIn("guest_access", denied.content.decode())
        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(Cart.objects.exists())
        self.assertEqual(CartRateLimitCounter.objects.get(scope="reset").count, 2)

    def test_store_outage_fails_closed_for_both_operations(self):
        with patch("cart.budget.consume_windows", side_effect=RateLimitStoreError("down")):
            csrf = _budget(CartAPIClient(), operation="csrf")
            reset = _budget(CartAPIClient(), operation="reset")
        for response in (csrf, reset):
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertNotIn("Retry-After", response)
        self.assertFalse(CartRateLimitCounter.objects.exists())
        self.assertFalse(GuestSession.objects.exists())

    def test_programming_and_configuration_failures_are_not_allowed(self):
        client = CartAPIClient()
        client.raise_request_exception = False
        with override_settings(DEBUG=False):
            for exc in (
                ProgrammingError("budget sql"),
                IntegrityError("budget row"),
                RuntimeError("budget"),
            ):
                with patch("cart.budget.consume_windows", side_effect=exc):
                    response = _budget(client)
                self.assertEqual(response.status_code, 500, type(exc))
                rendered = response.content.decode()
                self.assertNotIn("CART_BUDGET_ALLOWED", rendered)
                self.assertNotIn(TEST_SHOPPER_ADDRESS, rendered)
                self.assertNotIn(TEST_APP_CREDENTIAL, rendered)
                self.assertNotIn(TEST_HMAC_KEY, rendered)
        with patch(
            "cart.budget.consume_windows",
            side_effect=RateLimitNestedTransactionError("nested"),
        ):
            nested = _budget(CartAPIClient())
        self.assertEqual(nested.status_code, 503)
        self.assertEqual(nested.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
        self.assertNotEqual(nested.data["code"], "CART_BUDGET_ALLOWED")
        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(Cart.objects.exists())

    def test_budget_call_does_not_consume_a_cart_scope(self):
        response = _budget(CartAPIClient(), operation="reset")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(CartRateLimitCounter.objects.values_list("scope", flat=True)),
            ["reset"],
        )

    def test_json_policy_string_is_accepted(self):
        raw = (
            '{"csrf": [{"window_seconds": 60, "limit": 1}], '
            '"reset": [{"window_seconds": 60, "limit": 1}]}'
        )
        with _frozen_clock(), override_settings(CART_FRONTEND_RATE_LIMIT_POLICIES=raw):
            allowed = _budget(CartAPIClient())
            denied = _budget(CartAPIClient())
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 429)
        self.assertEqual(CartRateLimitCounter.objects.get(scope="csrf").count, 2)


@_enabled()
class BudgetSubjectTests(APITransactionTestCase):
    def test_existing_digests_stay_on_the_same_message(self):
        issuance = subject_key_for_address(scope="issuance", address=TEST_SHOPPER_ADDRESS)
        failed = subject_key_for_address(scope="failed_access", address=TEST_SHOPPER_ADDRESS)
        csrf = subject_key_for_address(scope="csrf", address=TEST_SHOPPER_ADDRESS)
        reset = subject_key_for_address(scope="reset", address="::ffff:" + TEST_SHOPPER_ADDRESS)
        session_id = uuid.uuid4()
        session_key = subject_key_for_guest_session(
            scope="authenticated_cart",
            session_id=session_id,
        )

        self.assertEqual(issuance, _manual_digest("issuance", "address", TEST_SHOPPER_ADDRESS))
        self.assertEqual(failed, _manual_digest("failed_access", "address", TEST_SHOPPER_ADDRESS))
        self.assertEqual(csrf, _manual_digest("csrf", "address", TEST_SHOPPER_ADDRESS))
        self.assertEqual(reset, _manual_digest("reset", "address", TEST_SHOPPER_ADDRESS))
        self.assertNotEqual(issuance, csrf)
        self.assertEqual(
            session_key,
            _manual_digest("authenticated_cart", "guest_session", str(session_id)),
        )
        with self.assertRaises(SubjectKeyError):
            subject_key_for_address(scope="authenticated_cart", address=TEST_SHOPPER_ADDRESS)
        with self.assertRaises(SubjectKeyError):
            subject_key_for_guest_session(scope="csrf", session_id=session_id)
        with self.assertRaises(SubjectKeyError):
            subject_key_for_guest_session(scope="reset", session_id=session_id)


@override_settings(DEBUG=False, CART_RATE_LIMIT_HMAC_KEY=TEST_HMAC_KEY)
@_enabled()
class BudgetReportTests(APITransactionTestCase):
    def test_production_traceback_hides_the_budget_subject(self):
        address = TEST_SHOPPER_ADDRESS
        digest = subject_key_for_address(scope="csrf", address=address)
        request = RequestFactory().post(
            "/api/cart/budget/",
            {"operation": "csrf"},
            content_type="application/json",
            HTTP_X_PHOENIX_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
            HTTP_X_PHOENIX_SHOPPER_ADDRESS=address,
        )
        with patch("cart.budget.consume_windows", side_effect=RuntimeError("budget report")):
            try:
                account_budget(address, "csrf")
            except RuntimeError:
                reporter = get_exception_reporter_class(request)(request, *sys.exc_info())
            else:
                self.fail("budget did not raise")
        text = reporter.get_traceback_text()
        self.assertNotIn(digest, text)
        self.assertNotIn(TEST_HMAC_KEY, text)
        self.assertNotIn(TEST_APP_CREDENTIAL, text)
        frames = {
            frame["function"]: frame.get("vars", [])
            for frame in reporter.get_traceback_data()["frames"]
        }
        self.assertIn("account_budget", frames)
        for _key, value in frames["account_budget"]:
            rendered = str(value)
            self.assertNotIn(digest, rendered)
            self.assertNotIn(address, rendered)
            self.assertIn(reporter.filter.cleansed_substitute, rendered)


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL budget concurrency only. Skipped on the normal SQLite run.",
)
@_enabled(csrf=3)
class BudgetPostgresTests(TransactionTestCase):
    def test_exactly_n_budget_requests_are_admitted(self):
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
                response = _budget(client, operation="csrf")
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
        self.assertEqual(sorted(results), [200, 200, 200, 429, 429, 429])
        self.assertEqual(GuestSession.objects.count(), 0)
        self.assertEqual(Cart.objects.count(), 0)
        self.assertEqual(CartRateLimitCounter.objects.get(scope="csrf").count, 6)


class FrontendBudgetMigrationTests(TransactionTestCase):
    def test_forward_upgrade_preserves_existing_counter_rows(self):
        executor = MigrationExecutor(connection)
        self.assertIn(
            ("cart", "0006_frontend_budget_scopes"),
            executor.loader.graph.nodes,
        )
        try:
            self._upgrade(executor)
        finally:
            MigrationExecutor(connection).migrate(
                MigrationExecutor(connection).loader.graph.leaf_nodes()
            )

    def _upgrade(self, executor):
        executor.migrate([("cart", "0005_rate_limit_counter")])
        apps = executor.loader.project_state([("cart", "0005_rate_limit_counter")]).apps
        Historical = apps.get_model("cart", "CartRateLimitCounter")
        start = timezone.now().replace(microsecond=0)
        expires_at = start + timedelta(minutes=65)
        subject_key = "ab" * 32
        row = Historical.objects.create(
            scope="issuance",
            subject_key=subject_key,
            window_seconds=60,
            window_start=start,
            count=4,
            expires_at=expires_at,
        )
        original_id = row.id

        executor.loader.build_graph()
        executor.migrate([("cart", "0006_frontend_budget_scopes")])

        stored = CartRateLimitCounter.objects.get(pk=original_id)
        self.assertEqual(stored.scope, "issuance")
        self.assertEqual(stored.subject_key, subject_key)
        self.assertEqual(stored.window_seconds, 60)
        self.assertEqual(stored.window_start, start)
        self.assertEqual(stored.count, 4)
        self.assertEqual(stored.expires_at, expires_at)
        self.assertEqual(CartRateLimitCounter.objects.count(), 1)

        CartRateLimitCounter.objects.create(
            scope="csrf",
            subject_key="cd" * 32,
            window_seconds=60,
            window_start=start,
            count=1,
            expires_at=expires_at,
        )
        self.assertEqual(CartRateLimitCounter.objects.get(pk=original_id).count, 4)
        self._assert_scope_constraint_lists_frontend_scopes()

    def _assert_scope_constraint_lists_frontend_scopes(self):
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conname = 'cart_rate_limit_counter_scope_valid'
                    """
                )
                definition = cursor.fetchone()[0]
            else:
                cursor.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table' AND name = 'cart_cartratelimitcounter'
                    """
                )
                definition = cursor.fetchone()[0]
        self.assertIn("csrf", definition)
        self.assertIn("reset", definition)
        self.assertIn("issuance", definition)
