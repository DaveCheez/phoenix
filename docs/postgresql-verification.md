# Isolated PostgreSQL verification

This runs the Django suite against a disposable local database. It does not
change order behaviour, and it does not use production credentials.

Production's PostgreSQL major version is not recorded in this repository.
The runner uses the official `postgres:16` image, so production-version
matching is still unverified.

## Requirements

- Local Docker Desktop, with the current context left unchanged.
- Docker Engine 28 or newer, so an explicit `127.0.0.1` publish stays on
  localhost. The runner checks the listening address and stops if it does not.
- The repository virtual environment at `../venv`.

## Run

From `backend`:

```text
..\venv\Scripts\python.exe scripts\run_postgres_verification.py
```

The script creates a new container, a local `CREATEDB` role, and a test
database. The published port is bound to `127.0.0.1` only. The official image's
default localhost `trust` lines are replaced with `scram-sha-256` before
tests connect. Passwords stay in that process and are not printed.

Before Django starts, the script prints the engine, host, port, database
name, and test database name.

The normal SQLite command is unchanged:

```text
..\venv\Scripts\python.exe manage.py test
```

PostgreSQL-only tests skip on that command.

## Settings

`base/settings_postgres_tests.py` loads only when
`PHOENIX_VANZ_POSTGRES_TESTS=1`. It ignores `DATABASE_URL`, refuses a
non-local host, and uses local email, cache, and file storage.

## Cleanup

The script removes the container it created, including that container's
anonymous data volume. It does not prune Docker or delete other resources.
