"""Bounded cart counter maintenance. The helper's delete SQL stays in place."""

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import ProgrammingError, connection
from django.test import TransactionTestCase, override_settings
from unittest.mock import patch

from orders.models import Order

from .management.commands.cart_rate_limit_maintenance import (
    ARGUMENT_ERROR,
    OBSERVATION_CAP,
    SCHEMA_ERROR,
    STORE_ERROR,
    Command as MaintenanceCommand,
)
from .models import Cart, CartRateLimitCounter, GuestSession
from .rate_limit import RateLimitStoreError, delete_expired_counters
from .test_application_client import TEST_APP_CREDENTIAL
from .test_rate_limit import TEST_HMAC_KEY


_COMMAND = "cart.management.commands.cart_rate_limit_maintenance"
_CUTOFF = datetime(2026, 6, 1, 12, 0, tzinfo=dt_timezone.utc)
_DIGEST = "ab" * 32
_OTHER_DIGEST = "cd" * 32
_GUEST_HASH = "ef" * 32


def _run(*args):
    stdout = StringIO()
    stderr = StringIO()
    try:
        call_command(
            "cart_rate_limit_maintenance",
            *args,
            stdout=stdout,
            stderr=stderr,
            skip_checks=True,
        )
    except CommandError as exc:
        return exc, stdout.getvalue(), stderr.getvalue()
    return None, stdout.getvalue(), stderr.getvalue()


def _summary(stdout):
    text = stdout.strip()
    parsed, end = json.JSONDecoder().raw_decode(text)
    if text[end:].strip():
        raise AssertionError("stdout contained more than one JSON value")
    return parsed


def _frozen():
    return patch(f"{_COMMAND}.accounting_now", return_value=_CUTOFF)


def _elapsed_clock(start, end):
    """First reading is the run start. Later readings, including waits, stay at the end."""
    state = {"calls": 0}

    def clock():
        state["calls"] += 1
        if state["calls"] == 1:
            return start
        return end

    return clock


def _row(*, subject, expires_at, window_start, count=3):
    return CartRateLimitCounter.objects.create(
        scope="issuance",
        subject_key=subject,
        window_seconds=60,
        window_start=window_start,
        count=count,
        expires_at=expires_at,
    )


def _restore_counter_table():
    connection.rollback()
    tables = set(connection.introspection.table_names())
    if CartRateLimitCounter._meta.db_table in tables:
        return
    with connection.schema_editor() as editor:
        editor.create_model(CartRateLimitCounter)


@override_settings(
    CART_RATE_LIMIT_ENABLED=False,
    CART_RATE_LIMIT_HMAC_KEY=TEST_HMAC_KEY,
    CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
    CART_RATE_LIMIT_POLICIES="",
    CART_FRONTEND_RATE_LIMIT_POLICIES="",
)
class CounterMaintenanceTests(TransactionTestCase):
    def test_default_and_dry_run_do_not_delete(self):
        expired = _row(
            subject=_DIGEST,
            window_start=_CUTOFF - timedelta(hours=2),
            expires_at=_CUTOFF - timedelta(minutes=1),
        )
        with patch(f"{_COMMAND}.delete_expired_counters", side_effect=AssertionError("deleted")):
            with _frozen():
                default_error, default_out, _default_err = _run()
                dry_error, dry_out, _dry_err = _run("--dry-run")
        self.assertIsNone(default_error)
        self.assertIsNone(dry_error)
        default = _summary(default_out)
        dry = _summary(dry_out)
        self.assertEqual(default["mode"], "read_only")
        self.assertEqual(dry["mode"], "dry_run")
        self.assertTrue(default["success"])
        self.assertTrue(dry["success"])
        self.assertEqual(default["rows_deleted"], 0)
        self.assertEqual(dry["rows_deleted"], 0)
        self.assertEqual(default["batches_attempted"], 0)
        self.assertEqual(default["stop_reason"], "read_only")
        self.assertEqual(default["observed_eligible_backlog"], 1)
        self.assertFalse(default["backlog_observation_capped"])
        self.assertTrue(CartRateLimitCounter.objects.filter(pk=expired.pk).exists())
        self.assertNotIn(_DIGEST, default_out + dry_out)
        self.assertNotIn(TEST_HMAC_KEY, default_out + dry_out)
        self.assertNotIn(TEST_APP_CREDENTIAL, default_out + dry_out)

    def test_invalid_arguments_do_not_write(self):
        expired = _row(
            subject=_DIGEST,
            window_start=_CUTOFF - timedelta(hours=2),
            expires_at=_CUTOFF - timedelta(minutes=1),
        )
        cases = (
            ["--dry-run", "--delete-expired"],
            ["--delete-expired", "--batch-size", "0"],
            ["--delete-expired", "--batch-size", "501"],
            ["--delete-expired", "--max-batches", "0"],
            ["--delete-expired", "--max-batches", "101"],
        )
        with patch(f"{_COMMAND}.delete_expired_counters", side_effect=AssertionError("deleted")):
            with patch(f"{_COMMAND}.accounting_now", side_effect=AssertionError("queried")):
                for args in cases:
                    error, stdout, stderr = _run(*args)
                    self.assertIsInstance(error, CommandError, args)
                    self.assertEqual(str(error), ARGUMENT_ERROR)
                    self.assertEqual(stdout, "")
                    self.assertNotIn(_DIGEST, stderr)
        self.assertTrue(CartRateLimitCounter.objects.filter(pk=expired.pk).exists())

    def test_only_rows_expired_at_the_cutoff_are_deleted(self):
        window_start = _CUTOFF - timedelta(days=2)
        equal = _row(subject=_DIGEST, window_start=window_start, expires_at=_CUTOFF, count=4)
        later = _row(
            subject=_OTHER_DIGEST,
            window_start=window_start,
            expires_at=_CUTOFF + timedelta(microseconds=1),
            count=5,
        )
        active = _row(
            subject="12" * 32,
            window_start=_CUTOFF,
            expires_at=_CUTOFF + timedelta(days=30),
            count=6,
        )
        cart = Cart.objects.create()
        guest = GuestSession.objects.create(
            token_hash=_GUEST_HASH,
            created_at=_CUTOFF,
            expires_at=_CUTOFF + timedelta(days=30),
        )
        orders_before = Order.objects.count()
        with _frozen():
            error, stdout, _stderr = _run(
                "--delete-expired",
                "--batch-size",
                "10",
                "--max-batches",
                "5",
            )
        self.assertIsNone(error)
        summary = _summary(stdout)
        self.assertEqual(summary["mode"], "delete_expired")
        self.assertEqual(summary["rows_deleted"], 1)
        self.assertEqual(summary["stop_reason"], "eligible_backlog_empty")
        self.assertEqual(summary["observed_eligible_backlog"], 0)
        self.assertFalse(summary["backlog_observation_capped"])
        self.assertIsNone(summary["oldest_expired_at"])
        self.assertIsNone(summary["oldest_expired_age_seconds"])
        self.assertFalse(CartRateLimitCounter.objects.filter(pk=equal.pk).exists())
        kept = CartRateLimitCounter.objects.get(pk=later.pk)
        self.assertEqual(kept.count, 5)
        self.assertEqual(kept.expires_at, _CUTOFF + timedelta(microseconds=1))
        untouched = CartRateLimitCounter.objects.get(pk=active.pk)
        self.assertEqual(untouched.count, 6)
        self.assertEqual(untouched.expires_at, _CUTOFF + timedelta(days=30))
        self.assertEqual(Cart.objects.get(pk=cart.pk).id, cart.id)
        self.assertEqual(GuestSession.objects.get(pk=guest.pk).token_hash, _GUEST_HASH)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertNotIn(_DIGEST, stdout)
        self.assertNotIn(_GUEST_HASH, stdout)
        self.assertNotIn(TEST_HMAC_KEY, stdout)

    def test_a_later_cutoff_can_collect_a_row_this_run_left_behind(self):
        window_start = _CUTOFF - timedelta(days=2)
        expires_at = _CUTOFF + timedelta(hours=1)
        row = _row(subject=_DIGEST, window_start=window_start, expires_at=expires_at)
        with _frozen():
            first_error, first_out, _stderr = _run("--delete-expired", "--batch-size", "5", "--max-batches", "2")
        self.assertIsNone(first_error)
        self.assertEqual(_summary(first_out)["rows_deleted"], 0)
        self.assertTrue(CartRateLimitCounter.objects.filter(pk=row.pk).exists())
        later_cutoff = expires_at
        with patch(f"{_COMMAND}.accounting_now", return_value=later_cutoff):
            second_error, second_out, _stderr = _run("--delete-expired", "--batch-size", "5", "--max-batches", "2")
        self.assertIsNone(second_error)
        self.assertEqual(_summary(second_out)["rows_deleted"], 1)
        self.assertFalse(CartRateLimitCounter.objects.filter(pk=row.pk).exists())

    def test_batch_cap_leaves_the_rest_and_reports_the_oldest_age(self):
        oldest_at = _CUTOFF - timedelta(seconds=90, microseconds=500000)
        middle_at = _CUTOFF - timedelta(seconds=10)
        newest_at = _CUTOFF - timedelta(seconds=1)
        rows = [
            _row(subject=f"{index:02x}" * 32, window_start=_CUTOFF - timedelta(days=3), expires_at=expires_at)
            for index, expires_at in enumerate((oldest_at, middle_at, newest_at))
        ]
        with _frozen():
            error, stdout, _stderr = _run("--delete-expired", "--batch-size", "1", "--max-batches", "2")
        self.assertIsNone(error)
        summary = _summary(stdout)
        self.assertEqual(summary["batches_attempted"], 2)
        self.assertEqual(summary["rows_deleted"], 2)
        self.assertEqual(summary["stop_reason"], "batch_limit_reached")
        self.assertEqual(summary["observed_eligible_backlog"], 1)
        self.assertFalse(summary["backlog_observation_capped"])
        self.assertEqual(summary["oldest_expired_age_seconds"], 1)
        self.assertEqual(summary["cutoff"], _CUTOFF.isoformat())
        self.assertGreaterEqual(summary["elapsed_seconds"], 0)
        self.assertEqual(CartRateLimitCounter.objects.count(), 1)
        self.assertTrue(CartRateLimitCounter.objects.filter(pk=rows[2].pk).exists())
        self.assertNotIn(_DIGEST, stdout)

    def test_capped_observation_is_not_reported_as_an_exact_total(self):
        window_start = datetime(2020, 1, 1, tzinfo=dt_timezone.utc)
        expires_at = _CUTOFF - timedelta(days=1)
        CartRateLimitCounter.objects.bulk_create(
            [
                CartRateLimitCounter(
                    scope="issuance",
                    subject_key=_DIGEST,
                    window_seconds=60,
                    window_start=window_start + timedelta(seconds=index),
                    count=1,
                    expires_at=expires_at,
                )
                for index in range(OBSERVATION_CAP + 1)
            ],
            batch_size=500,
        )
        with _frozen():
            error, stdout, _stderr = _run("--dry-run")
        self.assertIsNone(error)
        summary = _summary(stdout)
        self.assertEqual(summary["observed_eligible_backlog"], OBSERVATION_CAP)
        self.assertTrue(summary["backlog_observation_capped"])
        self.assertEqual(summary["rows_deleted"], 0)
        self.assertEqual(summary["oldest_expired_age_seconds"], 86400)
        self.assertNotIn(str(OBSERVATION_CAP + 1), stdout)
        self.assertEqual(CartRateLimitCounter.objects.count(), OBSERVATION_CAP + 1)
        self.assertNotIn(_DIGEST, stdout)

    def test_zero_deletion_does_not_claim_an_empty_backlog(self):
        _row(
            subject=_DIGEST,
            window_start=_CUTOFF - timedelta(hours=2),
            expires_at=_CUTOFF - timedelta(minutes=5),
        )
        with patch(f"{_COMMAND}.delete_expired_counters", return_value=0):
            with _frozen():
                error, stdout, _stderr = _run("--delete-expired", "--batch-size", "1", "--max-batches", "2")
        self.assertIsNone(error)
        summary = _summary(stdout)
        self.assertEqual(summary["batches_attempted"], 2)
        self.assertEqual(summary["rows_deleted"], 0)
        self.assertEqual(summary["stop_reason"], "batch_limit_reached")
        self.assertEqual(summary["observed_eligible_backlog"], 1)
        self.assertEqual(CartRateLimitCounter.objects.count(), 1)

    def test_later_batch_failure_keeps_the_committed_batch(self):
        first = _row(
            subject=_DIGEST,
            window_start=_CUTOFF - timedelta(days=2),
            expires_at=_CUTOFF - timedelta(hours=2),
        )
        second = _row(
            subject=_OTHER_DIGEST,
            window_start=_CUTOFF - timedelta(days=2, minutes=1),
            expires_at=_CUTOFF - timedelta(hours=1),
        )
        calls = {"count": 0}

        def flaky(**kwargs):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise RateLimitStoreError("secret-db-detail")
            return delete_expired_counters(**kwargs)

        with patch(f"{_COMMAND}.delete_expired_counters", side_effect=flaky):
            with patch(f"{_COMMAND}.time.monotonic", side_effect=_elapsed_clock(200.0, 200.5)):
                with _frozen():
                    error, stdout, stderr = _run("--delete-expired", "--batch-size", "1", "--max-batches", "5")
        self.assertIsInstance(error, CommandError)
        self.assertEqual(str(error), STORE_ERROR)
        summary = _summary(stdout)
        self.assertFalse(summary["success"])
        self.assertEqual(summary["error_category"], STORE_ERROR)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(summary["batches_attempted"], 2)
        self.assertEqual(summary["rows_deleted"], 1)
        self.assertEqual(summary["stop_reason"], "store_unavailable")
        self.assertIsNone(summary["observed_eligible_backlog"])
        self.assertIsNone(summary["backlog_observation_capped"])
        self.assertIsNone(summary["oldest_expired_at"])
        self.assertFalse(CartRateLimitCounter.objects.filter(pk=first.pk).exists())
        self.assertTrue(CartRateLimitCounter.objects.filter(pk=second.pk).exists())
        blob = stdout + stderr + str(error)
        self.assertNotIn("secret-db-detail", blob)
        self.assertNotIn(_DIGEST, blob)
        self.assertNotIn("SELECT", blob)
        self.assertEqual(
            summary,
            {
                "backlog_observation_capped": None,
                "batch_size": 1,
                "batches_attempted": 2,
                "cutoff": _CUTOFF.isoformat(),
                "elapsed_seconds": 0.5,
                "error_category": STORE_ERROR,
                "max_batches": 5,
                "mode": "delete_expired",
                "observed_eligible_backlog": None,
                "oldest_expired_age_seconds": None,
                "oldest_expired_at": None,
                "rows_deleted": 1,
                "stop_reason": "store_unavailable",
                "success": False,
            },
        )

    def test_first_deletion_invocation_raises_before_any_commit(self):
        row = _row(
            subject=_DIGEST,
            window_start=_CUTOFF - timedelta(hours=2),
            expires_at=_CUTOFF - timedelta(minutes=1),
        )
        calls = {"count": 0}

        def boom(**kwargs):
            calls["count"] += 1
            raise RateLimitStoreError("first-call-detail")

        with patch(f"{_COMMAND}.delete_expired_counters", side_effect=boom):
            with _frozen():
                error, stdout, stderr = _run("--delete-expired", "--batch-size", "1", "--max-batches", "4")
        self.assertEqual(str(error), STORE_ERROR)
        self.assertEqual(calls["count"], 1)
        summary = _summary(stdout)
        self.assertEqual(summary["batches_attempted"], 1)
        self.assertEqual(summary["rows_deleted"], 0)
        self.assertIsNone(summary["observed_eligible_backlog"])
        self.assertTrue(CartRateLimitCounter.objects.filter(pk=row.pk).exists())
        self.assertNotIn("first-call-detail", stdout + stderr + str(error))

    def test_probe_failure_after_a_committed_delete_is_not_another_attempt(self):
        row = _row(
            subject=_DIGEST,
            window_start=_CUTOFF - timedelta(hours=2),
            expires_at=_CUTOFF - timedelta(minutes=1),
        )
        calls = {"count": 0}

        def real_once(**kwargs):
            calls["count"] += 1
            return delete_expired_counters(**kwargs)

        def probe_fails(_cutoff):
            raise RateLimitStoreError("probe-detail")

        with patch(f"{_COMMAND}.delete_expired_counters", side_effect=real_once):
            with patch(f"{_COMMAND}._observe", side_effect=probe_fails):
                with _frozen():
                    error, stdout, stderr = _run(
                        "--delete-expired",
                        "--batch-size",
                        "1",
                        "--max-batches",
                        "1",
                    )
        self.assertEqual(str(error), STORE_ERROR)
        self.assertEqual(calls["count"], 1)
        summary = _summary(stdout)
        self.assertEqual(summary["batches_attempted"], 1)
        self.assertEqual(summary["rows_deleted"], 1)
        self.assertIsNone(summary["observed_eligible_backlog"])
        self.assertIsNone(summary["backlog_observation_capped"])
        self.assertIsNone(summary["oldest_expired_at"])
        self.assertFalse(CartRateLimitCounter.objects.filter(pk=row.pk).exists())
        self.assertNotIn("probe-detail", stdout + stderr + str(error))
        self.assertNotIn(_DIGEST, stdout)

    def test_zero_return_consumes_an_attempt_and_probes_before_empty(self):
        calls = {"deletes": 0, "probes": 0}
        real_observe = __import__(
            "cart.management.commands.cart_rate_limit_maintenance",
            fromlist=["_observe"],
        )._observe

        def counting_delete(**kwargs):
            calls["deletes"] += 1
            return delete_expired_counters(**kwargs)

        def counting_observe(cutoff):
            calls["probes"] += 1
            return real_observe(cutoff)

        with patch(f"{_COMMAND}.delete_expired_counters", side_effect=counting_delete):
            with patch(f"{_COMMAND}._observe", side_effect=counting_observe):
                with _frozen():
                    error, stdout, _stderr = _run("--delete-expired", "--batch-size", "5", "--max-batches", "3")
        self.assertIsNone(error)
        summary = _summary(stdout)
        self.assertEqual(calls["deletes"], 1)
        self.assertEqual(calls["probes"], 1)
        self.assertEqual(summary["batches_attempted"], 1)
        self.assertEqual(summary["rows_deleted"], 0)
        self.assertEqual(summary["stop_reason"], "eligible_backlog_empty")
        self.assertEqual(summary["observed_eligible_backlog"], 0)
        self.assertFalse(summary["backlog_observation_capped"])

    def test_elapsed_seconds_uses_monotonic_elapsed_time(self):
        with patch(f"{_COMMAND}.time.monotonic", side_effect=_elapsed_clock(100.0, 100.25)):
            with _frozen():
                error, stdout, _stderr = _run("--dry-run")
        self.assertIsNone(error)
        self.assertEqual(
            _summary(stdout),
            {
                "backlog_observation_capped": False,
                "batch_size": 500,
                "batches_attempted": 0,
                "cutoff": _CUTOFF.isoformat(),
                "elapsed_seconds": 0.25,
                "max_batches": 10,
                "mode": "dry_run",
                "observed_eligible_backlog": 0,
                "oldest_expired_age_seconds": None,
                "oldest_expired_at": None,
                "rows_deleted": 0,
                "stop_reason": "read_only",
                "success": True,
            },
        )

    def test_unrelated_programming_error_is_not_a_successful_cleanup(self):
        with patch(
            f"{_COMMAND}.accounting_now",
            side_effect=ProgrammingError("syntax error at statement boundary"),
        ):
            with self.assertRaises(ProgrammingError):
                error, stdout, _stderr = _run("--delete-expired")
                self.fail((error, stdout))
        self.assertFalse(CartRateLimitCounter.objects.exists())

    def test_store_failure_exits_nonzero_and_keeps_json_on_stdout(self):
        stdout = StringIO()
        stderr = StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            command = MaintenanceCommand()
            with patch(f"{_COMMAND}.accounting_now", side_effect=RateLimitStoreError("clock down")):
                with self.assertRaises(SystemExit) as raised:
                    command.run_from_argv(
                        [
                            "manage.py",
                            "cart_rate_limit_maintenance",
                            "--delete-expired",
                            "--skip-checks",
                        ]
                    )
        self.assertEqual(raised.exception.code, 1)
        summary = _summary(stdout.getvalue())
        self.assertFalse(summary["success"])
        self.assertEqual(summary["error_category"], STORE_ERROR)
        self.assertIsNone(summary["observed_eligible_backlog"])
        self.assertEqual(summary["batches_attempted"], 0)
        self.assertEqual(summary["rows_deleted"], 0)
        self.assertNotIn("{", stderr.getvalue())
        self.assertIn("CommandError: %s" % STORE_ERROR, stderr.getvalue())
        self.assertNotIn("clock down", stdout.getvalue() + stderr.getvalue())

    def test_missing_table_is_not_a_successful_empty_report(self):
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DROP TABLE %s" % connection.ops.quote_name(CartRateLimitCounter._meta.db_table)
                )
            with _frozen():
                error, stdout, stderr = _run()
            self.assertIsInstance(error, CommandError)
            self.assertIn(str(error), {SCHEMA_ERROR, STORE_ERROR})
            summary = _summary(stdout)
            self.assertFalse(summary["success"])
            self.assertNotEqual(summary["observed_eligible_backlog"], 0)
            self.assertIsNone(summary["observed_eligible_backlog"])
            blob = stdout + stderr + str(error)
            self.assertNotIn("no such table", blob.casefold())
            self.assertNotIn("does not exist", blob.casefold())
            self.assertNotIn("select", blob.casefold())
            self.assertNotIn(_DIGEST, blob)
        finally:
            _restore_counter_table()

    def test_store_outage_before_cutoff_reports_unknown_backlog(self):
        _row(
            subject=_DIGEST,
            window_start=_CUTOFF - timedelta(hours=2),
            expires_at=_CUTOFF - timedelta(minutes=1),
        )
        with patch(f"{_COMMAND}.accounting_now", side_effect=RateLimitStoreError("clock down")):
            error, stdout, stderr = _run("--delete-expired")
        self.assertEqual(str(error), STORE_ERROR)
        summary = _summary(stdout)
        self.assertFalse(summary["success"])
        self.assertIsNone(summary["cutoff"])
        self.assertEqual(summary["batches_attempted"], 0)
        self.assertEqual(summary["rows_deleted"], 0)
        self.assertIsNone(summary["observed_eligible_backlog"])
        self.assertEqual(CartRateLimitCounter.objects.count(), 1)
        self.assertNotIn("clock down", stdout + stderr + str(error))


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL overlapping cleanup only. Skipped on the normal SQLite run.",
)
class CounterMaintenancePostgresTests(TransactionTestCase):
    def test_overlapping_cleanups_do_not_double_count_or_delete_active_rows(self):
        expired_start = datetime(2020, 1, 1, tzinfo=dt_timezone.utc)
        expired = [
            _row(
                subject=f"{index + 1:02x}" * 32,
                window_start=expired_start + timedelta(seconds=index),
                expires_at=expired_start + timedelta(hours=1, minutes=index),
            )
            for index in range(4)
        ]
        active_start = datetime(2099, 1, 1, tzinfo=dt_timezone.utc)
        active = [
            _row(
                subject=f"{index + 20:02x}" * 32,
                window_start=active_start,
                expires_at=active_start + timedelta(days=10 + index),
                count=8,
            )
            for index in range(2)
        ]
        barrier = threading.Barrier(2)
        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                barrier.wait(15)
                error, stdout, _stderr = _run(
                    "--delete-expired",
                    "--batch-size",
                    "1",
                    "--max-batches",
                    "8",
                )
                with lock:
                    if error is not None:
                        errors.append(error)
                    else:
                        results.append(_summary(stdout))
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                thread_connection.close()

        workers = [threading.Thread(target=worker) for _ in range(2)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        deleted = 0
        for summary in results:
            self.assertTrue(summary["success"])
            self.assertLessEqual(summary["batches_attempted"], 8)
            self.assertLessEqual(summary["rows_deleted"], summary["batches_attempted"])
            deleted += summary["rows_deleted"]
        self.assertEqual(deleted, 4)
        self.assertFalse(CartRateLimitCounter.objects.filter(pk__in=[row.pk for row in expired]).exists())
        for row in active:
            stored = CartRateLimitCounter.objects.get(pk=row.pk)
            self.assertEqual(stored.count, 8)
            self.assertEqual(stored.expires_at, row.expires_at)
