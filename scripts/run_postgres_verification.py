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
_ORIGINAL_VENV = ROOT.parent / "venv"
_CREDENTIAL_KEYS = {
    "DATABASE_URL",
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
    "PGDATABASE",
    "PGPASSFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "BOTO_CONFIG",
}
ADMIN_USER = "phoenix_vanz_pgadmin"
ADMIN_DATABASE = "phoenix_vanz_pgtest"
APP_USER = "phoenix_vanz_app"
APP_DATABASE = "phoenix_vanz_pgtest"
TEST_DATABASE = "phoenix_vanz_pgtest_django"
CONTAINER = f"phoenix-vanz-pgtest-{os.getpid()}"
_HIDDEN = []


def _hide(*values):
    for value in values:
        if value and value not in _HIDDEN:
            _HIDDEN.append(value)


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


def _diagnostic(completed, secret_values):
    """Return a non-zero launch result without its command line or secrets."""
    rendered = "exit={}\n{}{}".format(
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    )
    return _redact(rendered, *secret_values)


def _controlled_process_env(child_env):
    """Environment for a Docker client. Inherited credentials are removed."""
    process_env = os.environ.copy()
    for key in list(process_env):
        if key in _CREDENTIAL_KEYS or key.startswith("AWS_"):
            process_env.pop(key, None)
    process_env.update(child_env)
    return process_env


def _env_name_options(flag, names, process_env):
    """Pass variable names only. Docker reads the values from process_env."""
    missing = [name for name in names if name not in process_env]
    if missing:
        raise SystemExit(
            "Refusing to launch. Missing environment names: "
            + ", ".join(sorted(missing))
        )
    options = []
    for name in names:
        options.extend([flag, name])
    return options


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


def _psql(container, admin_password, sql, extra_secrets=()):
    process_env = _controlled_process_env({"PGPASSWORD": admin_password})
    command = [
        "docker",
        "exec",
        *_env_name_options("-e", ["PGPASSWORD"], process_env),
        "-i",
        container,
        "psql",
        "-U",
        ADMIN_USER,
        "-d",
        ADMIN_DATABASE,
        "-v",
        "ON_ERROR_STOP=1",
    ]
    completed = _run(
        command,
        input=sql,
        capture_output=True,
        env=process_env,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "Database setup failed.\n"
            + _diagnostic(completed, [admin_password, *extra_secrets])
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
    process_env = _controlled_process_env({"PGPASSWORD": admin_password})
    completed = _run(
        [
            "docker",
            "exec",
            *_env_name_options("-e", ["PGPASSWORD"], process_env),
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
        env=process_env,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "Could not read PostgreSQL server facts.\n"
            + _diagnostic(completed, [admin_password])
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


def _refuse_original_venv(path_value):
    candidate = Path(path_value)
    original_python = _ORIGINAL_VENV / "Scripts" / "python.exe"
    for original in (_ORIGINAL_VENV, original_python):
        try:
            if candidate.resolve() == original.resolve():
                raise SystemExit("Refusing the original virtual environment.")
        except OSError:
            continue


def _postgres_container_launch(port, admin_password):
    """Start PostgreSQL. The password stays in the client environment."""
    child_env = {
        "POSTGRES_USER": ADMIN_USER,
        "POSTGRES_PASSWORD": admin_password,
        "POSTGRES_DB": ADMIN_DATABASE,
        "POSTGRES_HOST_AUTH_METHOD": "scram-sha-256",
    }
    process_env = _controlled_process_env(child_env)
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        CONTAINER,
        "--publish",
        f"{HOST_BIND}:{port}:5432",
        *_env_name_options("--env", list(child_env), process_env),
        IMAGE,
    ]
    return command, process_env


def _django_command(args, child_env, process_env):
    """Run Django with the upgraded interpreter, never the original venv."""
    image = os.environ.get("PHOENIX_VANZ_DOCKER_IMAGE", "").strip()
    if image:
        venv = os.environ.get("PHOENIX_VANZ_VENV", "").strip()
        if not venv:
            raise SystemExit(
                "PHOENIX_VANZ_VENV must name the upgraded virtual environment."
            )
        _refuse_original_venv(venv)
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            f"container:{CONTAINER}",
            "-v",
            f"{ROOT}:/work",
            "-v",
            f"{venv}:/opt/venv",
            "-w",
            "/work",
            "-e",
            "AWS_EC2_METADATA_DISABLED=true",
            "-e",
            "AWS_SHARED_CREDENTIALS_FILE=/aws-missing/credentials",
            "-e",
            "AWS_CONFIG_FILE=/aws-missing/config",
            *_env_name_options("-e", list(child_env), process_env),
            image,
            "/opt/venv/bin/python",
            *args,
        ]
        return command, f"docker:{image} venv={venv}"

    python = os.environ.get("PHOENIX_VANZ_PYTHON", "").strip()
    if not python:
        raise SystemExit(
            "Set PHOENIX_VANZ_DOCKER_IMAGE and PHOENIX_VANZ_VENV, "
            "or PHOENIX_VANZ_PYTHON, to the upgraded interpreter. "
            "The original ../venv interpreter is not used."
        )
    _refuse_original_venv(python)
    return [python, *args], python


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
    _hide(admin_password, app_password)
    created = False
    try:
        postgres_command, postgres_env = _postgres_container_launch(port, admin_password)
        started = _run(
            postgres_command,
            capture_output=True,
            env=postgres_env,
        )
        if started.returncode != 0:
            raise SystemExit(
                "Could not start PostgreSQL.\n"
                + _diagnostic(started, [admin_password, app_password])
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
            extra_secrets=(app_password,),
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

        docker_image = os.environ.get("PHOENIX_VANZ_DOCKER_IMAGE", "").strip()
        django_port = "5432" if docker_image else str(port)
        child_env = {
            "DJANGO_SETTINGS_MODULE": "base.settings_postgres_tests",
            "PHOENIX_VANZ_POSTGRES_TESTS": "1",
            "PHOENIX_VANZ_POSTGRES_HOST": HOST_BIND,
            "PHOENIX_VANZ_POSTGRES_PORT": django_port,
            "PHOENIX_VANZ_POSTGRES_NAME": APP_DATABASE,
            "PHOENIX_VANZ_POSTGRES_TEST_NAME": TEST_DATABASE,
            "PHOENIX_VANZ_POSTGRES_USER": APP_USER,
            "PHOENIX_VANZ_POSTGRES_PASSWORD": app_password,
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        host_env = _controlled_process_env(child_env)
        _say(f"django_port={django_port}")
        _say("RUN orders")
        orders_command, interpreter = _django_command(
            ["manage.py", "test", "orders", "--verbosity", "2"],
            child_env,
            host_env,
        )
        _say(f"interpreter={interpreter}")
        orders = _run(orders_command, cwd=ROOT, env=host_env)
        _say(f"orders_exit={orders.returncode}")
        _say("RUN full")
        full_command, _interpreter = _django_command(
            ["manage.py", "test", "--verbosity", "1"],
            child_env,
            host_env,
        )
        full = _run(full_command, cwd=ROOT, env=host_env)
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
    except SystemExit as exc:
        if isinstance(exc.code, str):
            raise SystemExit(_redact(exc.code, *_HIDDEN)) from None
        raise
    except Exception as exc:
        raise SystemExit(
            _redact(f"PostgreSQL verification stopped: {exc}", *_HIDDEN)
        ) from None
