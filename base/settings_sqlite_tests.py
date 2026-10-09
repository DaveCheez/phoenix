"""Opt-in settings for disposable SQLite test runs.

Importing this module does nothing unless PHOENIX_VANZ_SQLITE_TESTS=1.
It hides DATABASE_URL, uses an in-memory database, and keeps files, email
and cache local. It does not read a project .env file for secrets.
"""

import os
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


if os.environ.get("PHOENIX_VANZ_SQLITE_TESTS") != "1":
    raise ImproperlyConfigured(
        "Isolated SQLite tests require PHOENIX_VANZ_SQLITE_TESTS=1."
    )

import base.env as env_module


_REAL_CONFIG = env_module.config
_SYNTHETIC_SECRET = "sqlite-test-only-not-a-production-secret"


def _config(option, default=None, cast=None):
    """Hide production database and storage credentials from this process."""
    if option == "DATABASE_URL":
        value = ""
    elif option == "DEBUG_SETTING":
        value = True
    elif option == "DJANGO_SECRET_KEY":
        value = _SYNTHETIC_SECRET
    elif option in {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_STORAGE_BUCKET_NAME",
        "AWS_S3_ENDPOINT_URL",
        "AWS_S3_REGION_NAME",
        "AWS_S3_CUSTOM_DOMAIN",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
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


if os.environ.get("DATABASE_URL", "").strip():
    raise ImproperlyConfigured(
        "Isolated SQLite tests refuse an inherited DATABASE_URL."
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {"NAME": ":memory:"},
    }
}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "phoenix-vanz-sqlite-tests",
    }
}
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="phoenix-vanz-sqlite-media-"))
MEDIA_URL = "/media/"
