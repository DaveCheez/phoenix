# Cart counter maintenance

`manage.py cart_rate_limit_maintenance` reports expired
`CartRateLimitCounter` rows and, only when asked, deletes a bounded number
of them. It is not a scheduler, a public route, or a customer quota.

Nothing in deployment or CI runs this command. Emitting JSON does not mean
a monitor is watching it.

## Commands

Read-only, which is the default:

```text
manage.py cart_rate_limit_maintenance
manage.py cart_rate_limit_maintenance --dry-run
```

Neither calls `delete_expired_counters`. `--dry-run` and the default differ
only in the reported `mode` (`read_only` or `dry_run`).

Deletion:

```text
manage.py cart_rate_limit_maintenance --delete-expired
manage.py cart_rate_limit_maintenance --delete-expired --batch-size 500 --max-batches 10
```

`--dry-run` and `--delete-expired` cannot be combined. Invalid arguments
exit non-zero with `cart_rate_limit_maintenance_arguments_invalid` and do
not query or delete.

`--batch-size` defaults to 500 and must be 1..500. `--max-batches` defaults
to 10 and must be 1..100. These bound this maintenance process. They are
not shopper quotas. There is no mode that drains the table without a cap,
no caller-supplied cutoff, and no database URL argument.

The command uses the same default Django connection as
`delete_expired_counters`. It does not offer `--database`. The counter
table must already exist. This command does not create it and does not run
migrations.

`CART_RATE_LIMIT_ENABLED`, the HMAC key, and the application credential are
not required. The command never reads a subject digest in order to delete
an already expired row.

## Output fields

A finished run writes one JSON object to stdout. `sort_keys` is on, so the
field order is alphabetical.

| Field | Meaning |
| --- | --- |
| `success` | `true` only when the run finished without a store or schema failure. |
| `mode` | `read_only`, `dry_run`, or `delete_expired`. |
| `cutoff` | UTC timestamp sampled once from `accounting_now()` at the start. `null` if that sample failed. |
| `batch_size` | Configured delete batch size, including on a read-only run. |
| `max_batches` | Configured maximum number of delete calls. |
| `batches_attempted` | `delete_expired_counters` invocations this run started. The count increases before the call, so a call that raises is included. Read-only runs report `0`. This is not the number of calls that returned. |
| `rows_deleted` | Sum of the counts returned by calls that completed. A call that raises adds nothing, because that outcome is unknown. Read-only runs report `0`. |
| `observed_eligible_backlog` | Matching rows seen by the latest probe. `null` when the probe did not finish. |
| `backlog_observation_capped` | `true` when the probe stopped at 5000 rows because at least one more matched. `null` when the probe did not finish. |
| `oldest_expired_at` | Oldest observed `expires_at`, or `null`. |
| `oldest_expired_age_seconds` | Whole seconds from that timestamp to the cutoff, truncated toward zero. `null` when there is no observed row. |
| `stop_reason` | Why the run stopped. |
| `elapsed_seconds` | Elapsed time from `time.monotonic()` at the start of the run until the summary is built. Waiting is included. This is not CPU time from `time.process_time()`, and it does not stop the run. |
| `error_category` | Present only on failure. |

`stop_reason` is one of:

- `read_only` — no delete call was made.
- `eligible_backlog_empty` — a delete call returned zero and a fresh probe saw no eligible row.
- `batch_limit_reached` — the invocation used its `max_batches` calls. The backlog field says what remained. This reason does not by itself mean rows remain, and it does not mean every counter was cleaned.
- `store_unavailable` — the counter query or a delete call failed with a store error.
- `schema_unavailable` — the counter table is missing.

When `backlog_observation_capped` is `true`, `observed_eligible_backlog` is
5000 and is only a lower bound. It is not a table total. When it is
`false`, the probe saw every row that matched the cutoff at that moment.
A later insert is not included.

The probe reads `expires_at` only, ordered by `expires_at` then primary key,
and stops after 5001 matches. It does not print row ids, subject digests,
addresses, or credentials. A missing table or a failed query is an error,
not an empty backlog.

## Cutoff

One UTC cutoff is taken from the counter database clock at the start. Every
batch and every probe in that invocation uses that same value. A row whose
`expires_at` is later than the cutoff stays for a later run, which will
sample its own cutoff.

`expires_at` already includes the five-minute storage grace added when the
counter row was created. Maintenance does not add more grace and does not
move expiry, counts, or subject keys.

The helper treats `expires_at` equal to the cutoff as expired
(`expires_at <= cutoff`). A later timestamp is not eligible.

## Deletion

Only `--delete-expired` writes. Each batch is:

```text
delete_expired_counters(batch_size=..., now=cutoff)
```

The helper opens and commits its own transaction. This command does not wrap
those calls in another atomic block. `batches_attempted` increases before
each call. If that call raises, it still counts as an attempt and adds
nothing to `rows_deleted`. Earlier calls that already returned stay
committed. The failure output does not claim those commits were rolled
back, and it does not guess how many rows the failed call deleted.

A call that returns zero is counted toward `max_batches`. It is not proof
that the backlog is empty. The command probes again. If that probe still
sees eligible rows, it continues until the batch cap. It stops with
`eligible_backlog_empty` only when the probe itself sees none.

The run then stops. It does not retry a failed batch.

## Concurrent runs

Two overlapping invocations can select the same candidate rows. That
duplicate selection is expected. Deletion still goes through the helper:
it deletes by the selected primary keys and checks `expires_at` again, so
an active row is not deleted and one committed delete is not counted twice.
There is no new distributed lock in this checkpoint.

## Bounds are not a time limit

`batch_size`, `max_batches`, and the 5000-row probe limit how many rows this
process asks for. They do not cancel a query or a lock wait. A delete can
sit until the database or the operating system stops it. `elapsed_seconds`
records that wait after the fact. It is not a deadline. This checkpoint
does not change global database timeout settings, and it does not claim
that SQL execution is time-bounded.

## What may be deleted

Only `CartRateLimitCounter` rows already expired at the run's cutoff. Guest
sessions, carts, orders, and unexpired counters are not eligible. The
command cannot target another table.

## Failure

A store or missing-schema failure writes the JSON summary to stdout with
`success` false, then exits non-zero. The stderr category is one of:

- `cart_rate_limit_store_unavailable`
- `cart_rate_limit_schema_unavailable`

Backlog fields are `null` when that observation was not obtained. The
output does not include the exception, SQL text, or a connection string.
Programming errors that are not a missing counter table are not turned
into a successful summary.

## Later scheduling

Before this command is put on a timer, operators still need to:

- record when a run last completed with `success` true;
- watch `observed_eligible_backlog` and `oldest_expired_age_seconds` across runs;
- alert when a run fails or no run is recorded;
- choose the cadence, batch size, and batch cap from the rate at which rows are created and from the backlog that remains;
- treat a scheduler success as incomplete when the JSON still reports a backlog;
- set operational timeouts outside this command before any production schedule.

No App Platform job, cron, or worker is configured by this repository for
that work.
