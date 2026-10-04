"""Opt-in settings for the disposable local PostgreSQL test database.

Importing this module does nothing unless PHOENIX_VANZ_POSTGRES_TESTS=1.
It does not read DATABASE_URL and it refuses any non-local database host.
"""

import os
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


if os.environ.get("PHOENIX_VANZ_POSTGRES_TESTS") != "1":
    raise ImproperlyConfigured(
        "Isolated PostgreSQL tests require PHOENIX_VANZ_POSTGRES_TESTS=1."
    )

import base.env as env_module


_REAL_CONFIG = env_module.config
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _config(option, default=None, cast=None):
    """Hide production database and storage credentials from this process."""
    if option == "DATABASE_URL":
        value = ""
    elif option == "DEBUG_SETTING":
        value = True
    elif option == "DJANGO_SECRET_KEY":
        value = "postgres-test-only-not-a-production-secret"
    elif option in {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_STORAGE_BUCKET_NAME",
        "AWS_S3_ENDPOINT_URL",
        "AWS_S3_REGION_NAME",
        "AWS_S3_CUSTOM_DOMAIN",
    }:
        value = ""
    else:
        if cast is None:
            return _REAL_CONFIG(option, default=default)
        return _REAL_CONFIG(option, default=default, cast=cast)
    if cast is not None:
        return cast(value)
    return value


env_module.config = _config

from base.settings import *  # noqa: E402,F401,F403


def _required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"Isolated PostgreSQL tests require {name}.")
    return value


_HOST = _required("PHOENIX_VANZ_POSTGRES_HOST")
if _HOST not in _LOCAL_HOSTS:
    raise ImproperlyConfigured(
        "Isolated PostgreSQL tests refuse a non-local database host."
    )

_PORT = _required("PHOENIX_VANZ_POSTGRES_PORT")
if not _PORT.isdigit() or not (1 <= int(_PORT) <= 65535):
    raise ImproperlyConfigured("Isolated PostgreSQL tests require a numeric local port.")

_NAME = _required("PHOENIX_VANZ_POSTGRES_NAME")
_TEST_NAME = _required("PHOENIX_VANZ_POSTGRES_TEST_NAME")
_USER = _required("PHOENIX_VANZ_POSTGRES_USER")
_PASSWORD = _required("PHOENIX_VANZ_POSTGRES_PASSWORD")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": _HOST,
        "PORT": _PORT,
        "NAME": _NAME,
        "USER": _USER,
        "PASSWORD": _PASSWORD,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TEST": {"NAME": _TEST_NAME},
    }
}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "phoenix-vanz-postgres-tests",
    }
}
DEFAULT_FILE_STORAGE = "django.core.files.storage.FileSystemStorage"
MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="phoenix-vanz-pgtest-media-"))
MEDIA_URL = "/media/"
