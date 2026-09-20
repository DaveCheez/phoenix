import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from base.env import config


BASE_DIR = Path(__file__).resolve().parent.parent


def csv_setting(name: str, default: str = "") -> list[str]:
    """Read a comma-separated environment variable into a clean list."""
    raw_value = config(name, default=default)
    return [item.strip() for item in str(raw_value).split(",") if item.strip()]


DEBUG = config("DEBUG_SETTING", default=False, cast=bool)

SECRET_KEY = config("DJANGO_SECRET_KEY", default="")
if not SECRET_KEY or str(SECRET_KEY).startswith("REPLACE_"):
    if DEBUG:
        SECRET_KEY = "unsafe-development-key-only"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be configured for production deployments."
        )

ALLOWED_HOSTS = csv_setting(
    "DJANGO_ALLOWED_HOSTS",
    default="localhost,127.0.0.1,.ondigitalocean.app",
)

# DigitalOcean exposes the primary app domain through APP_DOMAIN.
app_domain = os.getenv("APP_DOMAIN", "").strip()
if app_domain and app_domain not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(app_domain)


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "nested_admin",
    "rest_framework",
    "accounts",
    "cart",
    "storages",
    "store",
    "orders",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "base.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "store.views.categories",
            ],
        },
    },
]

WSGI_APPLICATION = "base.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Override the local SQLite database when DATABASE_URL is configured.
from .db import *  # noqa: E402,F403


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles-cdn"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Uploaded product/category media is stored in DigitalOcean Spaces in production.
if not DEBUG:
    from cdn.conf import *  # noqa: E402,F403

SITE_URL = config(
    "SITE_URL",
    default=os.getenv("APP_URL", "http://127.0.0.1:8000"),
).rstrip("/")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = csv_setting(
    "CORS_ALLOWED_ORIGINS",
    default=(
        "http://localhost:3000,"
        "https://phoenixvanz.com,"
        "https://www.phoenixvanz.com"
    ),
)
CORS_ALLOWED_ORIGIN_REGEXES = [r"^https://[-a-z0-9]+\.ondigitalocean\.app$"]

CSRF_TRUSTED_ORIGINS = csv_setting(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default=(
        "https://phoenixvanz.com,"
        "https://www.phoenixvanz.com,"
        "https://api.phoenixvanz.com,"
        "https://dev.phoenixvanz.com"
    ),
)

# App Platform terminates TLS before forwarding requests to Django.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
