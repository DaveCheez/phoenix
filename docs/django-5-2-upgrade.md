# Django 5.2 upgrade verification

This records the isolated compatibility upgrade from Django 4.1.13 to
Django 5.2.18. It is not a DigitalOcean deployment test.

## Environment

| Item | Value |
| --- | --- |
| Branch | `upgrade/django-5-2` |
| Base commit | `7bc85397c2f0287226cf1fb790b6270ab8951c34` |
| Worktree | `backend-django-5-2` |
| Acceptance interpreter | CPython 3.11.15, `3.11.15 (main, Aug  5 2026, 03:57:30) [GCC 14.2.0]` |
| Official image | `python:3.11.15`, digest `sha256:d0199e2a90bf7a206a485b115323a75bc946f30b463d704c5435a454aca084dd` |
| Local image | `phoenix-vanz-django52-py311:local`, id `sha256:0cf5287526e448129d2db7037b0749cdda274aac97ab702150855a6e4659be2c` |
| libpq in that image | `17.11-0+deb13u1` (`libpq5` and `libpq-dev`) |
| Virtual environment | `phoenix-vanz-django52-venv`, invoked as `/opt/venv/bin/python` |
| PostgreSQL used for tests | official `postgres:16`, server `16.15 (Debian 16.15-1.pgdg13+2)` |
| Baseline process | Django 4.1.13 in `phoenix-vanz-django41-baseline-venv`, same local image |

The host has Python 3.13 only. It orchestrated Docker. It did not run the
acceptance suite. No global interpreter was installed.

`runtime.txt` still says `python-3.11.15`. That buildpack was not executed, so
this is not exact DigitalOcean runtime parity. Production's PostgreSQL major
version is still unrecorded.

`psycopg2==2.9.13` was built from source in the acceptance environment as
`psycopg2-2.9.13-cp311-cp311-linux_x86_64.whl`. `psycopg2-binary` was not
installed. Reported module version: `2.9.13 (dt dec pq3 ext lo64)`.

## Pins

`pip check` reported no broken requirements. Direct pins:

| Package | Was | Now | Reason |
| --- | --- | --- | --- |
| Django | 4.1.13 | 5.2.18 | Requested target. Requires `asgiref>=3.8.1` and `sqlparse>=0.3.1`. |
| djangorestframework | 3.14.0 | 3.18.3 | Requested target. 3.18.2 security fixes remain. 3.18.3 restores the previous `order_by_precedence` behaviour. |
| sqlparse | 0.4.4 | 0.6.0 | Requested target. |
| asgiref | 3.7.2 | 3.11.1 | Django 5.2 needs `>=3.8.1`. 3.11.1 includes the WsgiToAsgi duplicate-header fix. Later 3.12 releases change async behaviour this WSGI app does not need. |
| django-cors-headers | 4.3.1 | 4.7.0 | 4.7.0 is the release that declares Django 5.2 support. The project already uses `CORS_ALLOWED_ORIGINS`. |
| django-storages | 1.13.2 | 1.14.6 | 1.14.5 contains the CVE-2024-39330 path-traversal hardening. 1.14.6 is the current release. Its published classifiers stop at Django 5.1; Django 5.2 is only in the unreleased changelog. The existing `S3Boto3Storage` import still resolves. |
| django-nested-admin | 4.1.1 | 4.1.6 | 4.1.2 is the Django 5.2 compatibility fix. 4.1.6 is current and includes later prefix fixes. PyPI classifiers do not clearly list Django 5.2 or Python 3.11; the changelog and the admin page check are the support evidence. |
| whitenoise | 6.6.0 | 6.12.0 | Django 5.2 support is declared from 6.9.0. 6.12.0 fixes unauthorised file access in autorefresh mode. |
| Pillow | 10.4.0 | 12.3.0 | 10.4.0 has current image-parser advisories. 12.3.0 is the clean current release and has a Python 3.11 wheel. |
| urllib3 | 1.26.20 | 2.8.0 | 1.26.20 has current advisories. botocore 1.29.109 cannot accept urllib3 2. |
| boto3 / botocore | 1.26.109 / 1.29.109 | 1.34.162 / 1.34.162 | Earliest sampled botocore line that allows urllib3 2 on Python 3.10+. |
| s3transfer | 0.6.0 | 0.10.4 | Required by boto3 1.34.162 (`>=0.10,<0.11`). |
| psycopg2 | unpinned | 2.9.13 | Requested pin. Built from source against libpq. Not the binary package. |

Unchanged direct pins: `dj-database-url==1.3.0`, `gunicorn==23.0.0`,
`jmespath==1.0.1`, `python-dateutil==2.8.2`, `python-decouple==3.8`,
`six==1.16.0`, `typing-extensions==4.12.2`, `tzdata==2024.1`.

The commented `psycopg2-binary` line was removed so the two packages are not
both suggested. Resolver-only packages, not added as direct pins:
`python-monkey-business==1.1.0` and `packaging==26.3`.

The baseline environment left `psycopg2` and Django 4.1's `pytz` unpinned, so
that separate environment resolved `psycopg2==2.9.13` and `pytz==2026.5`.
Those floats were not copied into this requirements file.

## DRF 3.18 error shape

3.18 returns list-serializer errors as a dict keyed by invalid indexes when
`LIST_SERIALIZER_ERRORS_AS_DICT` is true, which is the 3.18.3 default. The
serializers here use `many=True` for output, including
`SerializerMethodField` data. They do not validate writable list input, so no
public envelope change and no `REST_FRAMEWORK` override were made.

## Storage

Before, `DEBUG` true left media on Django's filesystem default and set
`STATICFILES_STORAGE` to WhiteNoise's compressed manifest storage. `DEBUG`
false imported `cdn.conf`, which set `DEFAULT_FILE_STORAGE` to
`cdn.backends.MediaRootS3BotoStorage`. Both removed names are gone in
Django 5.1.

After, `base/settings.py` sets `STORAGES["default"]` to
`django.core.files.storage.FileSystemStorage` and `STORAGES["staticfiles"]`
to `whitenoise.storage.CompressedManifestStaticFilesStorage`. When `DEBUG` is
false, `cdn/conf.py` replaces the whole mapping: default stays
`cdn.backends.MediaRootS3BotoStorage`, and the WhiteNoise staticfiles alias
is repeated so the import does not drop it.

Unchanged production media behaviour: bucket default `phoenixvanz`, endpoint
`https://fra1.digitaloceanspaces.com`, region `fra1`, custom domain default
empty, cache control `max-age=86400`, ACL `public-read`, querystring auth
off, overwrite off, object prefix `media`. `StaticRootS3BotoStorage` remains
unused.

Subprocess checks imported both branches with a clean environment and an
in-memory database. Production media resolved to the Spaces backend, not
local disk. The worktree `db.sqlite3` was not created.

## Migrations

`manage.py makemigrations --check --dry-run` reported no changes.
`CheckConstraint(check=...)` still loads. On 5.2 its deconstruct form uses
`condition`, and `check=` emits `RemovedInDjango60Warning`. The autodetector
did not ask for a replacement migration, so none was created.

A disposable fresh database applied the committed chain under Django 5.2.18
through `sessions.0001_initial`. A second disposable database was migrated
with Django 4.1.13, seeded, then opened with Django 5.2.18. The pending plan
was empty. Seeded catalogue, configured cart, guest access, counter and
finalised order rows stayed usable. Deposit `550.00` plus balance `1100.00`
equaled full total `1650.00`. Configured unit price stayed `825.00` for
quantity 2. A negative counter update was rejected and the row stayed
unchanged. Identifier comparisons used digests and were not printed.

The read-only maintenance command against that upgraded database reported
success, mode `read_only`, one observed eligible backlog row, zero deletions,
and stop reason `read_only`. A second verify still matched.

## Tests

SQLite, `base.settings_sqlite_tests`, in-memory database: 301 tests, OK,
15 skipped. Every skip is an existing PostgreSQL-only test.

PostgreSQL, disposable database, including the concurrency tests: 301 tests,
OK, 1 skipped. The skip is the SQLite-specific reference-column assertion.

Deprecations, separate from failures: `RemovedInDjango60Warning` for
`CheckConstraint.check` in models and migrations, and a WhiteNoise warning
that `/work/staticfiles-cdn/` does not exist during tests. No test was
removed or skipped to hide a failure.

## Admin and static files

`collectstatic` used the WhiteNoise manifest backend and wrote 212 files to
a temporary directory, with 562 post-processed. Representative hashed admin
and nested-admin assets were present.

Gunicorn 23.0.0 on Linux/Python 3.11, `DEBUG=False`, served those assets and
the product nested-admin, order changelist and cart changelist pages for a
disposable staff user. Secure-cookie flags were turned off only in that
disposable settings module so the local HTTP check could log in. Production
settings still set those flags from `not DEBUG`. No Spaces upload was made.

## Browser

An isolated Nuxt workspace on `http://127.0.0.1:3001` talked to this upgraded
process on `http://127.0.0.1:8011`. The normal frontend `.env` was not
edited, and the listeners on ports 3000 and 8000 were left running.

Browser HTTP on that origin: start a new basket, select LWB (`+£130.00`),
add, open the basket, reload it. After reload the server values were base
unit `£695.00`, selected options `£130.00`, configured unit `£825.00`,
quantity 1, line total `£825.00`, and option text `Wheelbase: LWB`.

The existing budget-denial coverage stubs `fetch` in frontend unit tests. It
was not reused against this live process, and the frontend was not changed
to make that possible. No budget-denial result is claimed.

## Runner arguments

`scripts/run_postgres_verification.py` previously put
`PHOENIX_VANZ_POSTGRES_PASSWORD` and the PostgreSQL role passwords in Docker
arguments as `NAME=value`. It now passes `-e NAME` or `--env NAME` and keeps
the synthetic values in the environment of that Docker client process.
Failure text is redacted. This does not hide container environment values
from someone who controls the local Docker daemon.

The Windows host orchestrates with `py -3`. Django commands inside the runner
use `phoenix-vanz-django52-py311:local` and `/opt/venv/bin/python`. See
`docs/postgresql-verification.md` for the exact commands.

## What remains unproven

- DigitalOcean's `python-3.11.15` buildpack.
- Production PostgreSQL's major version.
- A released django-storages classifier for Django 5.2.
- A real Spaces upload.
- Production secure-cookie behaviour over HTTPS.
- Python 3.13 as an acceptance runtime.
