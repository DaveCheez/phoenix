from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email


NON_DELIVERY_EMAIL_BACKENDS = {
    "django.core.mail.backends.console.EmailBackend",
    "django.core.mail.backends.locmem.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend",
    "django.core.mail.backends.filebased.EmailBackend",
}
SMTP_EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
DEVELOPMENT_CONTACT_PROXY_SECRET = "local-development-contact-proxy-secret"


def validate_contact_proxy_secret(*, debug, secret):
    configured_secret = str(secret).strip()
    if not debug and (
        not configured_secret
        or configured_secret == DEVELOPMENT_CONTACT_PROXY_SECRET
    ):
        raise ImproperlyConfigured(
            "CONTACT_PROXY_SECRET must be configured for production."
        )


def validate_production_email_configuration(
    *,
    debug,
    email_backend,
    email_host,
    email_port,
    email_use_tls,
    email_use_ssl,
    email_host_user,
    email_host_password,
    default_from_email,
    contact_recipient_email,
):
    if debug:
        return

    if not email_backend:
        raise ImproperlyConfigured(
            "EMAIL_BACKEND must be configured for production."
        )
    if email_backend in NON_DELIVERY_EMAIL_BACKENDS:
        raise ImproperlyConfigured(
            "EMAIL_BACKEND must use a delivery backend in production."
        )
    if email_use_tls and email_use_ssl:
        raise ImproperlyConfigured(
            "EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be enabled."
        )

    for setting_name, value in (
        ("DEFAULT_FROM_EMAIL", default_from_email),
        ("CONTACT_RECIPIENT_EMAIL", contact_recipient_email),
    ):
        try:
            validate_email(str(value).strip())
        except ValidationError as exc:
            raise ImproperlyConfigured(
                f"{setting_name} must be a valid email address in production."
            ) from exc

    if email_backend == SMTP_EMAIL_BACKEND:
        missing = []
        if not str(email_host).strip():
            missing.append("EMAIL_HOST")
        if not isinstance(email_port, int) or email_port <= 0:
            missing.append("EMAIL_PORT")
        if not str(email_host_user).strip():
            missing.append("EMAIL_HOST_USER")
        if not str(email_host_password):
            missing.append("EMAIL_HOST_PASSWORD")
        if missing:
            raise ImproperlyConfigured(
                "SMTP email configuration is incomplete: " + ", ".join(missing)
            )
