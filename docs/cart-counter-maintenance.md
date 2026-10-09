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

## Proposed schedule

Nothing in this repository starts this command. The checked-in App Platform
examples (`.do/app.yaml` and `.do/app-production.example.yaml`) contain one
`PRE_DEPLOY` migrate job and the `django-api` service. They do not contain a
scheduled cleanup job. Those files are templates: they pin
`DaveCheez/pheonix` on `main`, `source_dir: /`, the Python buildpack
(`environment_slug: python`, `runtime.txt` is Python 3.11.15), and
`buildpack-stack=ubuntu-22`. There is no Dockerfile. They are not a copy of
a verified live app.

`manage.py` is at the root of this backend git repository. The template's
`source_dir: /` matches that layout. A future job must use the same
repository, branch, and deployment as the API that is being released. Do not
point it at `feature/order-foundation` or any other unmerged branch. App
Platform builds each component from that source. This plan does not claim the
job reuses the API container image.

The existing `migrate` pre-deploy job is what applies schema, including
`cart_cartratelimitcounter`, before a deployment is considered live. Cleanup
must not be the process that creates that table. A scheduled run belongs to
the deployment that already migrated.

The template puts environment variables on the app, and DigitalOcean
documents app-level variables as available to every component, with a
component-level variable of the same name winning. Variables set only on
`django-api` are not assumed to appear on a new job. Live values were not
read for this plan. Before any activation, confirm the job process itself
has these names:

- `DJANGO_SECRET_KEY` — required when `DEBUG_SETTING` is false, or Django
  refuses to start.
- `DEBUG_SETTING` — `False` for this job.
- `DATABASE_URL` — selects PostgreSQL. If it is absent, Django uses the
  container's ephemeral SQLite file and will not see the real counters.

The command does not read the cart HMAC key, application credential, or rate
limit policies. Do not copy payment, mail, or Spaces secrets onto the job
unless a staging boot shows Django cannot load without them. With
`DEBUG_SETTING` false, media settings are imported and accept empty defaults.

Proposed command, for review only:

```yaml
# NOT ACTIVATED. Do not paste this into .do/app.yaml until the rollout below
# is approved. Values are proposals.
jobs:
  - name: cart-counter-maintenance
    kind: SCHEDULED
    schedule:
      cron: "*/15 * * * *"
      time_zone: UTC
    environment_slug: python
    github:
      repo: DaveCheez/pheonix
      branch: main
      deploy_on_push: true
    source_dir: /
    run_command: >-
      python manage.py cart_rate_limit_maintenance
      --delete-expired --batch-size 500 --max-batches 10
    instance_count: 1
    instance_size_slug: apps-s-1vcpu-0.5gb
```

That cron expression is every 15 minutes, which is the minimum interval
DigitalOcean documents for scheduled jobs. `instance_count: 1` is the
proposal so one tick does not start several copies. Whether the platform
waits for a previous invocation before starting the next one is not
documented here and is unresolved. The command itself tolerates overlap.

DigitalOcean documents that scheduled jobs are not routable, are billed only
while running, can be listed from the Activity tab and
`GET /v2/apps/{app_id}/job-invocations`, and can raise a "Failed job
invocation" alert. The checked-in spec enables only `DEPLOYMENT_FAILED` and
`DOMAIN_FAILED`. It has no log forwarding destination. The published job
object includes `termination.grace_period_seconds` (default 120, maximum
600), which is the wait between TERM and KILL, not a maximum runtime.
DigitalOcean also says a job deployment timeout can be configured and
defaults to 30 minutes. This pass did not find that timeout's field name on
the published jobs object, so no timeout field is proposed. Overlap control
and automatic retry of a failed invocation are likewise undocumented and
unresolved.

The batch cap is still not a deadline. Two limits are proposed and not
implemented:

- Process deadline: once its spec field is identified, set the job's
  deployment timeout to 10 minutes. The documented default is 30 minutes.
  `termination.grace_period_seconds` does not provide that deadline.
- Database: PostgreSQL `statement_timeout` of 30 seconds and `lock_timeout`
  of 10 seconds on this job's connection only. Django's current database
  settings do not set either. Do not change the cluster-wide parameters in
  order to bound this command.

## Monitoring proposal

Three different failures need three different checks. None of them is
running.

1. Failed invocation. The process exits non-zero, the container fails to
   start, or the platform stops it. DigitalOcean documents a "Failed job
   invocation" alert, delivered by email or Slack. That is the smallest
   detector for this case. It does not, by itself, notice a job that never
   started, and it does not read the JSON backlog. The checked-in spec does
   not enable this alert. The recipient is not chosen here.

2. Missing invocation. The 15-minute schedule produced no completed run.
   No checked-in monitor compares invocation history with the clock. The
   smallest next step is a separate read of `job-invocations` for
   `cart-counter-maintenance`: alert when the newest completed invocation is
   older than 20 minutes. Twenty minutes is one missed slot plus a short
   grace, not an approved setting. Store that observation outside this
   Django process. Do not treat a quiet API as proof the cleanup ran.

3. Persistent backlog. Runs finish, including with `success` true, while
   expired rows remain or get older. A platform success is not an empty
   backlog. Read the JSON on stdout:

   - `rows_deleted` is only work a delete call confirmed.
   - `batches_attempted` includes a call that started and then raised.
   - `backlog_observation_capped` true means `observed_eligible_backlog` is
     5000 and only a lower bound.
   - `stop_reason` `batch_limit_reached` can leave rows behind. Look at the
     observed backlog; do not treat the stop reason as "clean".
   - After an error, backlog fields are null. Null is unknown, not zero.

   Proposed thresholds, not approved: alert when two consecutive successful
   runs still have `observed_eligible_backlog` greater than 0 and
   `oldest_expired_age_seconds` greater than 3600, or when any run reports
   `backlog_observation_capped` true. An hour is much longer than the
   15-minute cadence and is a sign the cap is not keeping up. No service is
   connected to parse this JSON yet.

## Rollout

Do this in order. Stopping later at any step leaves the API's cart
authentication and the existing counters unchanged.

1. Keep the maintenance command and this write-up free of a schedule. The
   command commit does not activate a job.
2. Deploy the candidate release to staging and confirm `migrate` has created
   the counter table.
3. Run `cart_rate_limit_maintenance` with no flags, or with `--dry-run`, and
   read one JSON object.
4. Run one staging `--delete-expired` against known expired fixture rows.
   Confirm active counters, guest sessions, carts, and orders are unchanged.
5. Prove the three monitors above against a forced non-zero exit, a skipped
   slot, and a run that stops on `batch_limit_reached` with rows still
   present.
6. Approve the cadence, batch size, batch cap, the 10-minute process
   deadline, the database timeouts, the expected cost, and who receives
   alerts.
7. Add the scheduled job only as part of the coordinated production release
   of that same revision.

Destroying the job component stops the schedule. It does not turn cart
authentication off, and it does not reset or restore counters. Do not restore
a database backup just to undo a counter delete. Cleanup never writes guest
sessions, carts, or orders.
