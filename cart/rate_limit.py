"""Internal fixed-window counter accounting.

This service does not serve HTTP and does not enforce customer quotas.
Routes do not call it yet.

Windows are fixed, UTC, and aligned to the Unix epoch:
``[start, start + window_seconds)``. An attempt sampled exactly at the end
of a window belongs to the next window. The timestamp is read once from the
counter database. A lock wait that crosses a boundary does not move the
attempt into the later window, and this is not a rolling-window guarantee.

Every requested window is incremented, including when the decision is a
denial. The increments commit before this function returns. A later cart or
issuance transaction can still roll back without refunding the attempt.
A connection lost around COMMIT can leave the outcome uncertain. Retrying
the call counts again. This is not exactly-once accounting.

A counter-store error is only a failure of this accounting transaction. It
is not the same thing as the whole application database being unavailable.

GuestSession, Cart, and Order rows are not locked here.
``expires_at`` is set to the window end plus five minutes when the row is
created and is not moved on later increments. The column does not delete
rows by itself. Cleanup has to be scheduled and watched before enforcement
ships. Per-subject rows are bounded by the scope and window limits in one
call. That is not a global cap on the table if cleanup is not running.
"""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import InterfaceError, OperationalError, connection, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .models import (
    RATE_LIMIT_MAX_WINDOW_SECONDS,
    RATE_LIMIT_SCOPES,
    CartRateLimitCounter,
)


_SUBJECT_KEY_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
_MAX_WINDOWS = 4
_EXPIRY_GRACE = timedelta(minutes=5)
_MAX_CLEANUP_BATCH = 500
# Signed 64-bit ceiling. Allowable limits stay strictly below it so a
# saturated count is always over every accepted limit.
SATURATION_CEILING = (2**63) - 1
_MAX_LIMIT = SATURATION_CEILING - 1
_STORE_MESSAGE = "Cart rate limit store is unavailable."


class RateLimitError(Exception):
    """Base error. Messages do not include subjects or secrets."""


class RateLimitValidationError(RateLimitError):
    pass


class RateLimitNestedTransactionError(RateLimitError):
    pass


class RateLimitStoreError(RateLimitError):
    pass


@dataclass(frozen=True)
class RateLimitWindow:
    """One trusted internal window. Not an HTTP parameter."""

    window_seconds: int
    limit: int


@dataclass(frozen=True)
class WindowDecision:
    window_seconds: int
    limit: int
    count: int
    window_start: datetime
    exhausted: bool


@dataclass(frozen=True)
class RateLimitDecision:
    """Committed outcome. ``retry_after_seconds`` is guidance, not a hold."""

    allowed: bool
    retry_after_seconds: int
    windows: tuple


@sensitive_variables()
def consume_windows(*, scope, subject_key, windows):
    """Increment each window and return the committed decision.

    The caller must not already be inside a transaction on the counter
    connection. Autocommit must be enabled. Nested use raises
    ``RateLimitNestedTransactionError`` before any counter write.
    """
    prepared = _validate_call(scope=scope, subject_key=subject_key, windows=windows)
    _require_own_transaction()
    try:
        with transaction.atomic(durable=True):
            sampled = accounting_now()
            outcomes = []
            for window in prepared:
                start = _window_start(sampled, window.window_seconds)
                expires_at = start + timedelta(seconds=window.window_seconds) + _EXPIRY_GRACE
                count = _increment_window(
                    scope=scope,
                    subject_key=subject_key,
                    window_seconds=window.window_seconds,
                    window_start=start,
                    expires_at=expires_at,
                )
                outcomes.append(
                    WindowDecision(
                        window_seconds=window.window_seconds,
                        limit=window.limit,
                        count=count,
                        window_start=start,
                        exhausted=count >= window.limit,
                    )
                )
            return _decision(outcomes, sampled)
    except (OperationalError, InterfaceError):
        raise RateLimitStoreError(_STORE_MESSAGE) from None


def delete_expired_counters(*, batch_size, now=None):
    """Delete at most ``batch_size`` expired counter rows.

    Only ``CartRateLimitCounter`` rows already past ``expires_at`` are
    eligible. Expiry is checked again on delete. This does not run from
    request handling.
    """
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise RateLimitValidationError("Cleanup batch size must be an integer.")
    if batch_size < 1 or batch_size > _MAX_CLEANUP_BATCH:
        raise RateLimitValidationError("Cleanup batch size is outside the supported range.")
    _require_own_transaction()
    try:
        with transaction.atomic(durable=True):
            moment = accounting_now() if now is None else _as_utc(now)
            selected = list(
                CartRateLimitCounter.objects.filter(expires_at__lte=moment)
                .order_by("expires_at", "id")
                .values_list("id", flat=True)[:batch_size]
            )
            if not selected:
                return 0
            deleted, _per_model = CartRateLimitCounter.objects.filter(
                id__in=selected,
                expires_at__lte=moment,
            ).delete()
            return deleted
    except (OperationalError, InterfaceError):
        raise RateLimitStoreError(_STORE_MESSAGE) from None


def accounting_now():
    """One UTC timestamp from the counter database, not from the caller."""
    try:
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute("SELECT clock_timestamp()")
            elif connection.vendor == "sqlite":
                cursor.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")
            else:
                raise RateLimitStoreError(_STORE_MESSAGE)
            row = cursor.fetchone()
    except (OperationalError, InterfaceError):
        raise RateLimitStoreError(_STORE_MESSAGE) from None
    if not row:
        raise RateLimitStoreError(_STORE_MESSAGE)
    return _as_utc(row[0])


def _validate_call(*, scope, subject_key, windows):
    if scope not in RATE_LIMIT_SCOPES:
        raise RateLimitValidationError("Scope is not supported.")
    if not isinstance(subject_key, str) or _SUBJECT_KEY_PATTERN.fullmatch(subject_key) is None:
        raise RateLimitValidationError("Subject key is not a 64-character digest.")
    if isinstance(windows, (str, bytes)) or not isinstance(windows, (list, tuple)):
        raise RateLimitValidationError("Windows must be a short list.")
    if len(windows) < 1 or len(windows) > _MAX_WINDOWS:
        raise RateLimitValidationError("Window collection is empty or too long.")
    prepared = []
    seen = set()
    for window in windows:
        if not isinstance(window, RateLimitWindow):
            raise RateLimitValidationError("Window descriptor is not valid.")
        seconds = _bounded_int(
            window.window_seconds,
            maximum=RATE_LIMIT_MAX_WINDOW_SECONDS,
            label="Window duration",
        )
        limit = _bounded_int(window.limit, maximum=_max_limit(), label="Window limit")
        if seconds in seen:
            raise RateLimitValidationError("Window durations must be unique.")
        seen.add(seconds)
        prepared.append(RateLimitWindow(window_seconds=seconds, limit=limit))
    prepared.sort(key=lambda item: item.window_seconds)
    return tuple(prepared)


def _bounded_int(value, *, maximum, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise RateLimitValidationError(f"{label} must be an integer.")
    if value < 1 or value > maximum:
        raise RateLimitValidationError(f"{label} is outside the supported range.")
    return value


def _max_limit():
    ceiling = SATURATION_CEILING
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 2:
        raise RateLimitValidationError("Saturation ceiling is not usable.")
    return ceiling - 1


def _require_own_transaction():
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RateLimitNestedTransactionError(
            "Rate limit accounting must open its own transaction."
        )


def _window_start(sampled, window_seconds):
    epoch = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)
    elapsed_us = (sampled - epoch) // timedelta(microseconds=1)
    if elapsed_us < 0:
        raise RateLimitValidationError("Accounting time is before the Unix epoch.")
    window_us = window_seconds * 1_000_000
    start_us = (elapsed_us // window_us) * window_us
    return epoch + timedelta(microseconds=start_us)


def _decision(outcomes, sampled):
    allowed = all(item.count <= item.limit for item in outcomes)
    if allowed:
        return RateLimitDecision(
            allowed=True,
            retry_after_seconds=0,
            windows=tuple(outcomes),
        )
    waits = []
    for item in outcomes:
        if not item.exhausted:
            continue
        end = item.window_start + timedelta(seconds=item.window_seconds)
        remaining = (end - sampled).total_seconds()
        waits.append(max(1, math.ceil(remaining)))
    if not waits:
        raise RateLimitValidationError("Denied decision had no exhausted window.")
    return RateLimitDecision(
        allowed=False,
        retry_after_seconds=max(waits),
        windows=tuple(outcomes),
    )


@sensitive_variables()
def _increment_window(*, scope, subject_key, window_seconds, window_start, expires_at):
    meta = CartRateLimitCounter._meta
    quote = connection.ops.quote_name
    table = quote(meta.db_table)
    scope_column = quote(meta.get_field("scope").column)
    subject_column = quote(meta.get_field("subject_key").column)
    seconds_column = quote(meta.get_field("window_seconds").column)
    start_column = quote(meta.get_field("window_start").column)
    count_column = quote(meta.get_field("count").column)
    expires_column = quote(meta.get_field("expires_at").column)
    sql = f"""
        INSERT INTO {table} (
            {scope_column}, {subject_column}, {seconds_column},
            {start_column}, {count_column}, {expires_column}
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT ({scope_column}, {subject_column}, {seconds_column}, {start_column})
        DO UPDATE SET {count_column} = CASE
            WHEN {table}.{count_column} >= %s THEN {table}.{count_column}
            ELSE {table}.{count_column} + 1
        END
        RETURNING {count_column}
    """
    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            [
                scope,
                subject_key,
                window_seconds,
                window_start,
                1,
                expires_at,
                SATURATION_CEILING,
            ],
        )
        row = cursor.fetchone()
    if row is None:
        raise RateLimitStoreError(_STORE_MESSAGE)
    return int(row[0])


def _as_utc(value):
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            return value.astimezone(dt_timezone.utc)
        return value.replace(tzinfo=dt_timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1]
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            return parsed.replace(tzinfo=dt_timezone.utc)
    raise RateLimitStoreError(_STORE_MESSAGE)
