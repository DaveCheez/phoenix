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
- The upgraded interpreter, named explicitly. The runner refuses the original
  checkout virtual environment at `../venv`.

## Run

The acceptance interpreter is Linux CPython 3.11.15 inside
`phoenix-vanz-django52-py311:local`. Its virtual environment is
`C:\Users\davec\Dev\Phoenix Vanz\phoenix-vanz-django52-venv`. That environment
has no usable Windows `Scripts\python.exe`. Do not set `PHOENIX_VANZ_PYTHON`
to one.

On Windows, `py -3` only orchestrates Docker. Django runs in the container.

PowerShell, from this worktree:

```powershell
$env:PHOENIX_VANZ_DOCKER_IMAGE = "phoenix-vanz-django52-py311:local"
$env:PHOENIX_VANZ_VENV = "C:\Users\davec\Dev\Phoenix Vanz\phoenix-vanz-django52-venv"
py -3 .\scripts\run_postgres_verification.py
```

The runner then mounts this worktree at `/work` and that virtual environment
at `/opt/venv`. The container command is `/opt/venv/bin/python`. The runner
sets `DJANGO_SETTINGS_MODULE=base.settings_postgres_tests` and
`PHOENIX_VANZ_POSTGRES_TESTS=1`. Database passwords are placed in that
process environment and passed to Docker as `-e NAME` / `--env NAME`. They
are not written into the argument list.

POSIX, when the shell is the orchestrator on a machine that can run Docker
and can see the same virtual-environment directory:

```sh
export PHOENIX_VANZ_DOCKER_IMAGE=phoenix-vanz-django52-py311:local
export PHOENIX_VANZ_VENV="/path/to/phoenix-vanz-django52-venv"
python3 scripts/run_postgres_verification.py
```

`PHOENIX_VANZ_PYTHON` is only for an interpreter that this orchestrating
operating system can execute directly. Inside the container that path is
`/opt/venv/bin/python`. It is not a Windows executable, and this repository's
Windows host cannot launch it.

In the Docker form, Django joins the database container's network namespace and
connects to `127.0.0.1:5432`. The published host port remains bound to
`127.0.0.1` only. The orchestrating Python is not the Django interpreter.

The original-environment guard compares the resolved `PHOENIX_VANZ_VENV` or
`PHOENIX_VANZ_PYTHON` path with exactly `../venv` and
`../venv/Scripts/python.exe`, relative to this checkout's parent. A path that
raises `OSError` during resolution is not treated as a match. The guard does
not refuse every file inside that virtual environment, a copy stored
elsewhere, or the host `py -3` interpreter. It also does not check that the
Docker image is the Python 3.11.15 image; the commands above name that image.

The script creates a new container, a local `CREATEDB` role, and a test
database. The published port is bound to `127.0.0.1` only. The official image's
default localhost `trust` lines are replaced with `scram-sha-256` before
tests connect. Passwords stay in that process and are not printed.

Before Django starts, the script prints the engine, host, port, database
name, and test database name.

The normal SQLite command uses the same image, mount, and in-container
interpreter. PowerShell:

```powershell
docker run --rm `
  -v "C:\Users\davec\Dev\Phoenix Vanz\phoenix-vanz-backend-digitalocean-ready\backend-django-5-2:/work" `
  -v "C:\Users\davec\Dev\Phoenix Vanz\phoenix-vanz-django52-venv:/opt/venv" `
  -w /work `
  -e DJANGO_SETTINGS_MODULE=base.settings_sqlite_tests `
  -e PHOENIX_VANZ_SQLITE_TESTS=1 `
  -e AWS_EC2_METADATA_DISABLED=true `
  -e AWS_SHARED_CREDENTIALS_FILE=/aws-missing/credentials `
  -e AWS_CONFIG_FILE=/aws-missing/config `
  -e PYTHONDONTWRITEBYTECODE=1 `
  phoenix-vanz-django52-py311:local `
  /opt/venv/bin/python manage.py test
```

POSIX equivalent, with the host path of this worktree and the Linux virtual
environment substituted:

```sh
docker run --rm \
  -v "/path/to/backend-django-5-2:/work" \
  -v "/path/to/phoenix-vanz-django52-venv:/opt/venv" \
  -w /work \
  -e DJANGO_SETTINGS_MODULE=base.settings_sqlite_tests \
  -e PHOENIX_VANZ_SQLITE_TESTS=1 \
  -e AWS_EC2_METADATA_DISABLED=true \
  -e AWS_SHARED_CREDENTIALS_FILE=/aws-missing/credentials \
  -e AWS_CONFIG_FILE=/aws-missing/config \
  -e PYTHONDONTWRITEBYTECODE=1 \
  phoenix-vanz-django52-py311:local \
  /opt/venv/bin/python manage.py test
```

PostgreSQL-only tests skip on that command.

## Settings

`base/settings_postgres_tests.py` loads only when
`PHOENIX_VANZ_POSTGRES_TESTS=1`. It ignores `DATABASE_URL`, refuses a
non-local host, and uses local email, cache, and file storage.

## Cleanup

The script removes the container it created, including that container's
anonymous data volume. It does not prune Docker or delete other resources.
