"""Bounded maintenance for expired cart rate-limit counters.

Default and ``--dry-run`` only report. ``--delete-expired`` is the only mode
that writes, and each batch is one call to ``delete_expired_counters``.
This command does not schedule itself.
"""

import json
import math
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import InterfaceError, OperationalError, ProgrammingError

from cart.models import CartRateLimitCounter
from cart.rate_limit import (
    RateLimitStoreError,
    accounting_now,
    delete_expired_counters,
)


OBSERVATION_CAP = 5000
DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_BATCHES = 10
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 500
MIN_BATCHES = 1
MAX_BATCHES = 100

ARGUMENT_ERROR = "cart_rate_limit_maintenance_arguments_invalid"
STORE_ERROR = "cart_rate_limit_store_unavailable"
SCHEMA_ERROR = "cart_rate_limit_schema_unavailable"

_COUNTER_TABLE = CartRateLimitCounter._meta.db_table


class Command(BaseCommand):
    help = (
        "Report expired cart rate-limit counters, or delete a bounded number "
        "of them. Read-only unless --delete-expired is set."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the eligible backlog and do not delete.",
        )
        parser.add_argument(
            "--delete-expired",
            action="store_true",
            help="Delete expired counter rows in bounded batches.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help="Expired rows requested per delete call. Permitted 1..500.",
        )
        parser.add_argument(
            "--max-batches",
            type=int,
            default=DEFAULT_MAX_BATCHES,
            help="Maximum delete calls in this invocation. Permitted 1..100.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        delete_expired = bool(options["delete_expired"])
        batch_size = options["batch_size"]
        max_batches = options["max_batches"]
        if dry_run and delete_expired:
            raise CommandError(ARGUMENT_ERROR)
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size < MIN_BATCH_SIZE
            or batch_size > MAX_BATCH_SIZE
        ):
            raise CommandError(ARGUMENT_ERROR)
        if (
            isinstance(max_batches, bool)
            or not isinstance(max_batches, int)
            or max_batches < MIN_BATCHES
            or max_batches > MAX_BATCHES
        ):
            raise CommandError(ARGUMENT_ERROR)

        mode = "delete_expired" if delete_expired else "dry_run" if dry_run else "read_only"
        started = time.monotonic()
        state = {
            "cutoff": None,
            "batches": 0,
            "deleted": 0,
        }
        try:
            state["cutoff"] = accounting_now()
            if delete_expired:
                reason, observation = _delete_bounded(
                    state,
                    batch_size=batch_size,
                    max_batches=max_batches,
                )
            else:
                observation = _observe(state["cutoff"])
                reason = "read_only"
        except RateLimitStoreError:
            self._fail(
                mode=mode,
                state=state,
                batch_size=batch_size,
                max_batches=max_batches,
                started=started,
                category=STORE_ERROR,
                stop_reason="store_unavailable",
            )
        except ProgrammingError as exc:
            if not _is_missing_counter_table(exc):
                raise
            self._fail(
                mode=mode,
                state=state,
                batch_size=batch_size,
                max_batches=max_batches,
                started=started,
                category=SCHEMA_ERROR,
                stop_reason="schema_unavailable",
            )
        except (OperationalError, InterfaceError) as exc:
            missing = _is_missing_counter_table(exc)
            self._fail(
                mode=mode,
                state=state,
                batch_size=batch_size,
                max_batches=max_batches,
                started=started,
                category=SCHEMA_ERROR if missing else STORE_ERROR,
                stop_reason="schema_unavailable" if missing else "store_unavailable",
            )

        summary = _summary(
            success=True,
            mode=mode,
            state=state,
            batch_size=batch_size,
            max_batches=max_batches,
            observation=observation,
            stop_reason=reason,
            started=started,
            error_category=None,
        )
        self.stdout.write(json.dumps(summary, sort_keys=True))

    def _fail(self, *, mode, state, batch_size, max_batches, started, category, stop_reason):
        summary = _summary(
            success=False,
            mode=mode,
            state=state,
            batch_size=batch_size,
            max_batches=max_batches,
            observation=None,
            stop_reason=stop_reason,
            started=started,
            error_category=category,
        )
        self.stdout.write(json.dumps(summary, sort_keys=True))
        raise CommandError(category) from None


def _delete_bounded(state, *, batch_size, max_batches):
    """Call the existing helper once per batch. Never wrap those calls."""
    cutoff = state["cutoff"]
    while state["batches"] < max_batches:
        state["batches"] += 1
        removed = delete_expired_counters(batch_size=batch_size, now=cutoff)
        state["deleted"] += removed
        if removed == 0:
            observation = _observe(cutoff)
            if observation["observed"] == 0 and not observation["capped"]:
                return "eligible_backlog_empty", observation
    return "batch_limit_reached", _observe(cutoff)


def _observe(cutoff):
    expires_at_values = list(
        CartRateLimitCounter.objects.filter(expires_at__lte=cutoff)
        .order_by("expires_at", "id")
        .values_list("expires_at", flat=True)[: OBSERVATION_CAP + 1]
    )
    capped = len(expires_at_values) > OBSERVATION_CAP
    visible = expires_at_values[:OBSERVATION_CAP]
    if not visible:
        return {"observed": 0, "capped": False, "oldest": None, "age": None}
    oldest = visible[0]
    return {
        "observed": OBSERVATION_CAP if capped else len(visible),
        "capped": capped,
        "oldest": oldest,
        "age": _age_seconds(cutoff, oldest),
    }


def _age_seconds(cutoff, expired_at):
    seconds = (cutoff - expired_at).total_seconds()
    if seconds < 0:
        return 0
    return int(math.floor(seconds))


def _summary(
    *,
    success,
    mode,
    state,
    batch_size,
    max_batches,
    observation,
    stop_reason,
    started,
    error_category,
):
    if observation is None:
        observed = None
        capped = None
        oldest = None
        age = None
    else:
        observed = observation["observed"]
        capped = observation["capped"]
        oldest = _iso(observation["oldest"])
        age = observation["age"]
    payload = {
        "success": success,
        "mode": mode,
        "cutoff": _iso(state["cutoff"]),
        "batch_size": batch_size,
        "max_batches": max_batches,
        "batches_attempted": state["batches"],
        "rows_deleted": state["deleted"],
        "observed_eligible_backlog": observed,
        "backlog_observation_capped": capped,
        "oldest_expired_at": oldest,
        "oldest_expired_age_seconds": age,
        "stop_reason": stop_reason,
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }
    if error_category is not None:
        payload["error_category"] = error_category
    return payload


def _iso(value):
    if value is None:
        return None
    return value.isoformat()


def _is_missing_counter_table(exc):
    current = exc
    for _ in range(5):
        if current is None:
            return False
        if getattr(current, "pgcode", None) == "42P01":
            return True
        if "undefinedtable" in current.__class__.__name__.casefold():
            return True
        text = str(current).casefold()
        if "no such table" in text and _COUNTER_TABLE.casefold() in text:
            return True
        current = current.__cause__ or current.__context__
    return False
