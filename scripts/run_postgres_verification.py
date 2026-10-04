"""Create a disposable local PostgreSQL container and run Django tests against it.

This does not read or print production credentials. It refuses to publish the
database on any address other than 127.0.0.1.
"""

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "postgres:16"
HOST_BIND = "127.0.0.1"
ADMIN_USER = "phoenix_vanz_pgadmin"
ADMIN_DATABASE = "phoenix_vanz_pgtest"
APP_USER = "phoenix_vanz_app"
APP_DATABASE = "phoenix_vanz_pgtest"
TEST_DATABASE = "phoenix_vanz_pgtest_django"
CONTAINER = f"phoenix-vanz-pgtest-{os.getpid()}"


def _say(message):
    print(message, flush=True)


def _run(args, **kwargs):
    return subprocess.run(args, check=False, text=True, **kwargs)


def _redact(text, *secrets_to_hide):
    redacted = text or ""
    for secret in secrets_to_hide:
        if secret:
            redacted = redacted.replace(secret, "***")
    return redacted


def _free_port():
    for port in range(55432, 55532):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((HOST_BIND, port))
            except OSError:
                continue
            return port
    raise SystemExit("No free localhost port was found in 55432-55531.")


def _listeners(port):
    completed = _run(["netstat", "-ano"], capture_output=True)
    if completed.returncode != 0:
        raise SystemExit("netstat failed while checking the published port.")
    matches = []
    needle = f":{port}"
    for line in completed.stdout.splitlines():
        if needle in line and "LISTENING" in line.upper():
            matches.append(line.strip())
    return matches


def _assert_localhost_only(port):
    matches = _listeners(port)
    if not matches:
        raise SystemExit(f"Published port {port} is not listening.")
    for line in matches:
        address = line.split()[1]
        if not (address.startswith(f"{HOST_BIND}:") or address.startswith("[::1]:")):
            raise SystemExit(
                "Docker published the database port outside localhost. "
                "The container was not left running.\n" + "\n".join(matches)
            )
    return matches


def _wait_until_ready(container):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        completed = _run(
            [
                "docker",
                "exec",
                container,
                "pg_isready",
                "-U",
                ADMIN_USER,
                "-d",
                ADMIN_DATABASE,
            ],
            capture_output=True,
        )
        if completed.returncode == 0:
            return
        time.sleep(1)
    raise SystemExit("PostgreSQL did not become ready inside the container.")


def _psql(container, admin_password, sql):
    completed = _run(
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={admin_password}",
            "-i",
            container,
            "psql",
            "-U",
            ADMIN_USER,
            "-d",
            ADMIN_DATABASE,
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input=sql,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "Database setup failed.\n"
            + _redact(completed.stderr, admin_password)
        )


def _lock_down_authentication(container, admin_password):
    """Replace the image's default trust lines before any test connects."""
    hba = (
        "local all all scram-sha-256\n"
        "host all all 127.0.0.1/32 scram-sha-256\n"
        "host all all ::1/128 scram-sha-256\n"
        "host all all 0.0.0.0/0 scram-sha-256\n"
        "host all all ::/0 scram-sha-256\n"
    )
    written = _run(
        ["docker", "exec", "-i", container, "sh", "-c", 'cat > "$PGDATA/pg_hba.conf"'],
        input=hba,
        capture_output=True,
    )
    if written.returncode != 0:
        raise SystemExit(
            "Could not replace pg_hba.conf.\n" + _redact(written.stderr, admin_password)
        )
    _psql(container, admin_password, "SELECT pg_reload_conf();")


def _server_facts(container, admin_password):
    completed = _run(
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={admin_password}",
            container,
            "psql",
            "-U",
            ADMIN_USER,
            "-d",
            ADMIN_DATABASE,
            "-At",
            "-c",
            "SHOW server_version",
            "-c",
            "SELECT type || ' ' || COALESCE(address, 'local') || ' ' || auth_method "
            "FROM pg_hba_file_rules ORDER BY line_number",
        ],
        capture_output=True,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "Could not read PostgreSQL server facts.\n"
            + _redact(completed.stderr, admin_password)
        )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise SystemExit("PostgreSQL did not return a server version.")
    version = lines[0]
    rules = lines[1:]
    if not rules or any(rule.endswith(" trust") or " trust" in f" {rule} " for rule in rules):
        raise SystemExit(
            "Authentication still allows trust. "
            f"rules={rules}"
        )
    return version, rules


def _remove_container(container):
    _run(["docker", "rm", "-f", "-v", container], capture_output=True)


def main():
    existing = _run(
        ["docker", "ps", "-aq", "--filter", f"name=^{CONTAINER}$"],
        capture_output=True,
    )
    if existing.stdout.strip():
        raise SystemExit(f"Refusing to reuse existing container {CONTAINER}.")

    port = _free_port()
    admin_password = secrets.token_urlsafe(24)
    app_password = secrets.token_urlsafe(24)
    created = False
    try:
        started = _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                CONTAINER,
                "--publish",
                f"{HOST_BIND}:{port}:5432",
                "--env",
                f"POSTGRES_USER={ADMIN_USER}",
                "--env",
                f"POSTGRES_PASSWORD={admin_password}",
                "--env",
                f"POSTGRES_DB={ADMIN_DATABASE}",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
                IMAGE,
            ],
            capture_output=True,
        )
        if started.returncode != 0:
            raise SystemExit(
                "Could not start PostgreSQL.\n" + _redact(started.stderr, admin_password)
            )
        created = True
        _wait_until_ready(CONTAINER)
        listeners = _assert_localhost_only(port)
        _lock_down_authentication(CONTAINER, admin_password)
        version, host_methods = _server_facts(CONTAINER, admin_password)
        _psql(
            CONTAINER,
            admin_password,
            f"""
            CREATE ROLE {APP_USER} LOGIN PASSWORD '{app_password}' CREATEDB NOSUPERUSER;
            GRANT CONNECT ON DATABASE {APP_DATABASE} TO {APP_USER};
            """,
        )
        _say(f"image={IMAGE}")
        _say(f"server_version={version}")
        _say(f"host_auth={','.join(host_methods)}")
        _say(f"container={CONTAINER}")
        _say("published=" + " | ".join(listeners))
        _say("engine=django.db.backends.postgresql")
        _say(f"host={HOST_BIND}")
        _say(f"port={port}")
        _say(f"name={APP_DATABASE}")
        _say(f"test_name={TEST_DATABASE}")
        _say(f"user={APP_USER}")

        child_env = os.environ.copy()
        for key in (
            "DATABASE_URL",
            "PGHOST",
            "PGPORT",
            "PGUSER",
            "PGPASSWORD",
            "PGDATABASE",
            "PGPASSFILE",
        ):
            child_env.pop(key, None)
        child_env.update(
            {
                "DJANGO_SETTINGS_MODULE": "base.settings_postgres_tests",
                "PHOENIX_VANZ_POSTGRES_TESTS": "1",
                "PHOENIX_VANZ_POSTGRES_HOST": HOST_BIND,
                "PHOENIX_VANZ_POSTGRES_PORT": str(port),
                "PHOENIX_VANZ_POSTGRES_NAME": APP_DATABASE,
                "PHOENIX_VANZ_POSTGRES_TEST_NAME": TEST_DATABASE,
                "PHOENIX_VANZ_POSTGRES_USER": APP_USER,
                "PHOENIX_VANZ_POSTGRES_PASSWORD": app_password,
            }
        )
        python = str(ROOT.parent / "venv" / "Scripts" / "python.exe")
        _say("RUN orders")
        orders = _run(
            [python, "manage.py", "test", "orders", "--verbosity", "2"],
            cwd=ROOT,
            env=child_env,
        )
        _say(f"orders_exit={orders.returncode}")
        _say("RUN full")
        full = _run(
            [python, "manage.py", "test", "--verbosity", "1"],
            cwd=ROOT,
            env=child_env,
        )
        _say(f"full_exit={full.returncode}")
        if orders.returncode != 0 or full.returncode != 0:
            raise SystemExit(
                f"PostgreSQL tests failed. orders={orders.returncode} full={full.returncode}"
            )
    finally:
        if created:
            _remove_container(CONTAINER)
            _say(f"removed_container={CONTAINER}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f"PostgreSQL verification stopped: {exc}") from exc
