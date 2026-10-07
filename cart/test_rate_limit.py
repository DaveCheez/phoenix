"""Counter foundation tests. Routes are not wired to this service."""

import logging
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.db import OperationalError, connection, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import Cart, CartRateLimitCounter
from .rate_limit import (
    SATURATION_CEILING,
    RateLimitDecision,
    RateLimitNestedTransactionError,
    RateLimitStoreError,
    RateLimitValidationError,
    RateLimitWindow,
    accounting_now,
    consume_windows,
    delete_expired_counters,
)
from .rate_limit_subjects import (
    SubjectKeyError,
    subject_key_for_address,
    subject_key_for_guest_session,
)
from .test_application_client import CartAPIClient, cart_app_settings


TEST_HMAC_KEY = "00112233445566778899aabbccddeeff" * 2
_HMAC = override_settings(CART_RATE_LIMIT_HMAC_KEY=TEST_HMAC_KEY)


def _window(seconds, limit):
    return RateLimitWindow(window_seconds=seconds, limit=limit)


def _address_key(scope="issuance", address="203.0.113.10"):
    return subject_key_for_address(scope=scope, address=address)


@_HMAC
class SubjectKeyTests(SimpleTestCase):
    def test_keys_are_domain_separated_and_stable(self):
        first = _address_key()
        again = _address_key()
        other_scope = _address_key(scope="failed_access")

        self.assertEqual(first, again)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, other_scope)
        self.assertNotIn("203.0.113.10", first)

    def test_ipv4_mapped_ipv6_matches_ipv4(self):
        self.assertEqual(
            _address_key(address="203.0.113.8"),
            _address_key(address="::ffff:203.0.113.8"),
        )

    def test_ipv6_is_grouped_at_slash_64(self):
        same = subject_key_for_address(scope="issuance", address="2001:db8::1")
        also_same = subject_key_for_address(scope="issuance", address="2001:db8::ffff")
        other = subject_key_for_address(scope="issuance", address="2001:db8:0:1::1")

        self.assertEqual(same, also_same)
        self.assertNotEqual(same, other)

    def test_guest_sessions_are_separate_from_addresses(self):
        first = uuid.uuid4()
        second = uuid.uuid4()
        address_key = _address_key()
        session_key = subject_key_for_guest_session(
            scope="authenticated_cart",
            session_id=first,
        )
        same_session = subject_key_for_guest_session(
            scope="authenticated_cart",
            session_id=str(first).upper(),
        )

        self.assertEqual(session_key, same_session)
        self.assertNotEqual(
            session_key,
            subject_key_for_guest_session(scope="authenticated_cart", session_id=second),
        )
        self.assertNotEqual(session_key, address_key)
        self.assertNotIn(str(first), session_key)

    def test_scope_and_subject_mismatches_are_rejected(self):
        with self.assertRaises(SubjectKeyError):
            subject_key_for_address(scope="authenticated_cart", address="203.0.113.10")
        with self.assertRaises(SubjectKeyError):
            subject_key_for_guest_session(scope="issuance", session_id=uuid.uuid4())

    def test_absent_hmac_configuration_is_rejected_without_a_fallback(self):
        with override_settings(CART_RATE_LIMIT_HMAC_KEY=""):
            with self.assertRaises(SubjectKeyError) as raised:
                _address_key()
        self.assertNotIn(TEST_HMAC_KEY, str(raised.exception))
        self.assertNotIn("203.0.113.10", str(raised.exception))

    def test_placeholder_hmac_keys_are_rejected(self):
        for value in ("replace-with-64-lowercase-hex-chars", "A" * 64, "0" * 64):
            with override_settings(CART_RATE_LIMIT_HMAC_KEY=value):
                with self.assertRaises(SubjectKeyError):
                    _address_key()


@cart_app_settings
class CounterRoutesStayUnwiredTests(APITestCase):
    def test_public_start_does_not_call_the_counter_service(self):
        with patch("cart.rate_limit.consume_windows", side_effect=AssertionError("wired")):
            response = CartAPIClient().post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertFalse(CartRateLimitCounter.objects.exists())


@_HMAC
class CounterAccountingTests(TransactionTestCase):
    def test_database_clock_is_utc(self):
        moment = accounting_now()
        self.assertTrue(timezone.is_aware(moment))
        self.assertEqual(moment.utcoffset(), timedelta(0))

    def test_invalid_arguments_do_not_write(self):
        key = _address_key()
        rejected = [
            dict(scope="other", subject_key=key, windows=[_window(60, 1)]),
            dict(scope="issuance", subject_key="abc", windows=[_window(60, 1)]),
            dict(scope="issuance", subject_key=key, windows=[]),
            dict(scope="issuance", subject_key=key, windows=[_window(60, 1)] * 5),
            dict(scope="issuance", subject_key=key, windows=[_window(True, 1)]),
            dict(scope="issuance", subject_key=key, windows=[_window(60, True)]),
            dict(scope="issuance", subject_key=key, windows=[_window(0, 1)]),
            dict(scope="issuance", subject_key=key, windows=[_window(86401, 1)]),
            dict(
                scope="issuance",
                subject_key=key,
                windows=[_window(60, 1), _window(60, 2)],
            ),
        ]
        for kwargs in rejected:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(RateLimitValidationError):
                    consume_windows(**kwargs)
        self.assertFalse(CartRateLimitCounter.objects.exists())

    def test_counts_increment_and_denial_keeps_the_attempt(self):
        key = _address_key()
        windows = [_window(60, 2)]
        first = consume_windows(scope="issuance", subject_key=key, windows=windows)
        second = consume_windows(scope="issuance", subject_key=key, windows=windows)
        denied = consume_windows(scope="issuance", subject_key=key, windows=windows)

        self.assertTrue(first.allowed)
        self.assertEqual(first.retry_after_seconds, 0)
        self.assertEqual(first.windows[0].count, 1)
        self.assertTrue(second.allowed)
        self.assertEqual(second.windows[0].count, 2)
        self.assertFalse(denied.allowed)
        self.assertGreater(denied.retry_after_seconds, 0)
        self.assertEqual(denied.windows[0].count, 3)
        self.assertEqual(CartRateLimitCounter.objects.get().count, 3)

    def test_reversed_windows_share_rows_and_denial_waits_for_the_longer_limit(self):
        key = _address_key(scope="failed_access")
        forward = [_window(10, 1), _window(60, 2)]
        backward = [_window(60, 2), _window(10, 1)]
        moment = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        with patch("cart.rate_limit.accounting_now", return_value=moment):
            allowed = consume_windows(scope="failed_access", subject_key=key, windows=forward)
            denied = consume_windows(scope="failed_access", subject_key=key, windows=backward)

        self.assertTrue(allowed.allowed)
        self.assertEqual([item.window_seconds for item in allowed.windows], [10, 60])
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.retry_after_seconds, 60)
        self.assertEqual(CartRateLimitCounter.objects.count(), 2)
        counts = {
            row.window_seconds: row.count
            for row in CartRateLimitCounter.objects.all()
        }
        self.assertEqual(counts, {10: 2, 60: 2})

    def test_exact_window_boundary_opens_the_next_window(self):
        key = _address_key()
        boundary = datetime(2026, 6, 1, 0, 0, 10, tzinfo=dt_timezone.utc)
        just_before = boundary - timedelta(microseconds=1)
        with patch("cart.rate_limit.accounting_now", return_value=just_before):
            consume_windows(scope="issuance", subject_key=key, windows=[_window(10, 5)])
        with patch("cart.rate_limit.accounting_now", return_value=boundary):
            later = consume_windows(
                scope="issuance",
                subject_key=key,
                windows=[_window(10, 5)],
            )

        self.assertEqual(later.windows[0].count, 1)
        self.assertEqual(later.windows[0].window_start, boundary)
        self.assertEqual(CartRateLimitCounter.objects.count(), 2)

    def test_expiry_is_not_extended(self):
        key = _address_key()
        moment = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        with patch("cart.rate_limit.accounting_now", return_value=moment):
            consume_windows(scope="issuance", subject_key=key, windows=[_window(30, 5)])
            first_expiry = CartRateLimitCounter.objects.get().expires_at
            consume_windows(scope="issuance", subject_key=key, windows=[_window(30, 5)])
        row = CartRateLimitCounter.objects.get()
        self.assertEqual(row.expires_at, first_expiry)
        self.assertEqual(row.count, 2)
        self.assertEqual(
            row.expires_at,
            moment + timedelta(seconds=30) + timedelta(minutes=5),
        )

    def test_saturation_does_not_overflow_or_reopen(self):
        key = _address_key()
        with patch("cart.rate_limit.SATURATION_CEILING", 3):
            windows = [_window(60, 2)]
            results = [
                consume_windows(scope="issuance", subject_key=key, windows=windows)
                for _ in range(4)
            ]
        self.assertEqual([item.windows[0].count for item in results], [1, 2, 3, 3])
        self.assertEqual([item.allowed for item in results], [True, True, False, False])
        self.assertEqual(CartRateLimitCounter.objects.get().count, 3)

    def test_later_window_failure_rolls_back_the_earlier_increment(self):
        key = _address_key()
        real = consume_windows.__globals__["_increment_window"]
        calls = {"n": 0}

        def fail_second(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OperationalError("down")
            return real(**kwargs)

        with patch("cart.rate_limit._increment_window", side_effect=fail_second):
            with self.assertRaises(RateLimitStoreError) as raised:
                consume_windows(
                    scope="issuance",
                    subject_key=key,
                    windows=[_window(10, 5), _window(60, 5)],
                )
        self.assertNotIn(key, str(raised.exception))
        self.assertFalse(CartRateLimitCounter.objects.exists())

    def test_nested_transaction_is_rejected_without_writes(self):
        key = _address_key()
        with transaction.atomic():
            with self.assertRaises(RateLimitNestedTransactionError):
                consume_windows(scope="issuance", subject_key=key, windows=[_window(60, 1)])
        self.assertFalse(CartRateLimitCounter.objects.exists())

    def test_disabled_autocommit_is_rejected_without_writes(self):
        key = _address_key()
        connection.set_autocommit(False)
        try:
            with self.assertRaises(RateLimitNestedTransactionError):
                consume_windows(scope="issuance", subject_key=key, windows=[_window(60, 1)])
        finally:
            connection.rollback()
            connection.set_autocommit(True)
        self.assertFalse(CartRateLimitCounter.objects.exists())

    def test_committed_counter_survives_a_later_business_rollback(self):
        key = _address_key()
        consume_windows(scope="issuance", subject_key=key, windows=[_window(60, 5)])
        try:
            with transaction.atomic():
                Cart.objects.create()
                raise RuntimeError("business")
        except RuntimeError:
            pass
        self.assertFalse(Cart.objects.exists())
        self.assertEqual(CartRateLimitCounter.objects.get().count, 1)

    def test_separate_sessions_do_not_share_an_authenticated_counter(self):
        first = subject_key_for_guest_session(scope="authenticated_cart", session_id=uuid.uuid4())
        second = subject_key_for_guest_session(scope="authenticated_cart", session_id=uuid.uuid4())
        windows = [_window(60, 1)]
        one = consume_windows(scope="authenticated_cart", subject_key=first, windows=windows)
        two = consume_windows(scope="authenticated_cart", subject_key=second, windows=windows)
        denied = consume_windows(scope="authenticated_cart", subject_key=first, windows=windows)

        self.assertTrue(one.allowed)
        self.assertTrue(two.allowed)
        self.assertFalse(denied.allowed)
        self.assertEqual(CartRateLimitCounter.objects.count(), 2)

    def test_cleanup_removes_only_expired_rows(self):
        past = datetime(2020, 1, 1, tzinfo=dt_timezone.utc)
        older = self._row(past, past + timedelta(minutes=1), "aa" * 32)
        newer = self._row(past + timedelta(hours=1), past + timedelta(hours=1, minutes=1), "bb" * 32)
        active = self._row(
            datetime(2099, 1, 1, tzinfo=dt_timezone.utc),
            datetime(2099, 1, 1, 0, 5, tzinfo=dt_timezone.utc),
            "cc" * 32,
        )
        Cart.objects.create()
        removed = delete_expired_counters(batch_size=1, now=datetime(2021, 1, 1, tzinfo=dt_timezone.utc))
        self.assertEqual(removed, 1)
        self.assertFalse(CartRateLimitCounter.objects.filter(pk=older.pk).exists())
        self.assertTrue(CartRateLimitCounter.objects.filter(pk=newer.pk).exists())
        self.assertTrue(CartRateLimitCounter.objects.filter(pk=active.pk).exists())
        self.assertEqual(Cart.objects.count(), 1)
        with self.assertRaises(RateLimitValidationError):
            delete_expired_counters(batch_size=True, now=past)

    def test_repr_and_logs_omit_subjects_and_the_secret(self):
        address = "203.0.113.44"
        key = _address_key(address=address)
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("cart")
        handler = _Capture()
        logger.addHandler(handler)
        try:
            decision = consume_windows(
                scope="issuance",
                subject_key=key,
                windows=[_window(60, 2)],
            )
        finally:
            logger.removeHandler(handler)
        row = CartRateLimitCounter.objects.get()
        visible = " ".join(records) + str(row) + repr(decision) + repr(row)
        self.assertNotIn(address, visible)
        self.assertNotIn(key, visible)
        self.assertNotIn(TEST_HMAC_KEY, visible)
        self.assertNotIn("subject_key", str(row))
        self.assertIsInstance(decision, RateLimitDecision)

    def _row(self, window_start, expires_at, subject_key):
        return CartRateLimitCounter.objects.create(
            scope="issuance",
            subject_key=subject_key,
            window_seconds=60,
            window_start=window_start,
            count=1,
            expires_at=expires_at,
        )


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL counter concurrency only. Skipped on the normal SQLite run.",
)
@_HMAC
class CounterPostgresConcurrencyTests(TransactionTestCase):
    def test_competing_first_inserts_do_not_lose_increments(self):
        key = _address_key()
        decisions = self._race(
            threads=8,
            windows=[_window(60, 5)],
            subject_key=key,
        )
        self.assertEqual(len(decisions), 8)
        self.assertEqual(sum(1 for item in decisions if item.allowed), 5)
        self.assertEqual(CartRateLimitCounter.objects.get().count, 8)
        self.assertEqual(
            sorted(item.windows[0].count for item in decisions),
            list(range(1, 9)),
        )

    def test_multi_window_calls_stay_atomic_across_subjects(self):
        first = _address_key(address="203.0.113.20")
        second = _address_key(address="203.0.113.21")
        self._race(threads=4, windows=[_window(60, 10), _window(10, 10)], subject_key=first)
        self._race(threads=3, windows=[_window(10, 10), _window(60, 10)], subject_key=second)

        first_counts = self._counts(first)
        second_counts = self._counts(second)
        self.assertEqual(first_counts, {10: 4, 60: 4})
        self.assertEqual(second_counts, {10: 3, 60: 3})

    def _race(self, *, threads, windows, subject_key):
        barrier = threading.Barrier(threads)
        decisions = []
        errors = []
        lock = threading.Lock()

        def worker():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                barrier.wait(15)
                decision = consume_windows(
                    scope="issuance",
                    subject_key=subject_key,
                    windows=windows,
                )
                with lock:
                    decisions.append(decision)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                thread_connection.close()

        workers = [threading.Thread(target=worker) for _ in range(threads)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(20)
        self.assertFalse(any(worker_thread.is_alive() for worker_thread in workers))
        self.assertEqual(errors, [])
        return decisions

    def _counts(self, subject_key):
        return {
            row.window_seconds: row.count
            for row in CartRateLimitCounter.objects.filter(subject_key=subject_key)
        }
