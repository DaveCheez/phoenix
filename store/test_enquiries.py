import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from base.contact_config import (
    parse_boolean_setting,
    validate_contact_proxy_secret,
    validate_production_email_configuration,
)
from store.admin import EnquiryAdmin
from store.models import ContactRateLimitBucket, Enquiry


VALID_ENQUIRY = {
    "name": "Jane Smith",
    "email": "jane@example.com",
    "phone": "01234 567890",
    "message": "Please quote a pop-top conversion.",
    "source_url": "https://phoenixvanz.com/contact",
    "website": "",
}

CONTACT_PROXY_SECRET = "test-contact-proxy-secret"
DEFAULT_CLIENT_IP = "203.0.113.10"
ENQUIRY_SETTINGS = {
    "CONTACT_PROXY_SECRET": CONTACT_PROXY_SECRET,
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "DEFAULT_FROM_EMAIL": "noreply@phoenixvanz.test",
    "CONTACT_RECIPIENT_EMAIL": "enquiries@phoenixvanz.test",
}
PRODUCTION_EMAIL_SETTINGS = {
    "debug": False,
    "email_backend": "django.core.mail.backends.smtp.EmailBackend",
    "email_host": "smtp.example.com",
    "email_port": 587,
    "email_use_tls": True,
    "email_use_ssl": False,
    "email_host_user": "smtp-user",
    "email_host_password": "smtp-password",
    "default_from_email": "noreply@phoenixvanz.test",
    "contact_recipient_email": "enquiries@phoenixvanz.test",
}


@override_settings(**ENQUIRY_SETTINGS)
class ContactEnquiryAPITests(APITestCase):
    def setUp(self):
        self.url = reverse("contact-enquiry-api")

    def proxy_headers(
        self,
        *,
        secret=CONTACT_PROXY_SECRET,
        client_ip=DEFAULT_CLIENT_IP,
        **extra,
    ):
        return {
            "HTTP_X_PHOENIX_CONTACT_SECRET": secret,
            "HTTP_X_PHOENIX_CLIENT_IP": client_ip,
            **extra,
        }

    def post_enquiry(
        self,
        payload=None,
        *,
        secret=CONTACT_PROXY_SECRET,
        client_ip=DEFAULT_CLIENT_IP,
        **extra,
    ):
        return self.client.post(
            self.url,
            VALID_ENQUIRY if payload is None else payload,
            format="json",
            **self.proxy_headers(
                secret=secret,
                client_ip=client_ip,
                **extra,
            ),
        )

    def test_valid_request_creates_enquiry_and_sends_email(self):
        response = self.post_enquiry()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["success"], True)
        self.assertEqual(response.data["code"], "ENQUIRY_RECEIVED")
        self.assertEqual(
            response.data["message"],
            "Thanks — your enquiry has been received. We will get back to you shortly.",
        )
        self.assertRegex(
            response.data["reference"],
            r"^PV-\d{8}-[A-F0-9]{16}$",
        )

        enquiry = Enquiry.objects.get()
        self.assertEqual(enquiry.reference, response.data["reference"])
        self.assertEqual(enquiry.name, "Jane Smith")
        self.assertEqual(enquiry.email, "jane@example.com")
        self.assertEqual(enquiry.phone, "01234 567890")
        self.assertEqual(enquiry.message, "Please quote a pop-top conversion.")
        self.assertEqual(enquiry.source_url, "https://phoenixvanz.com/contact")
        self.assertEqual(enquiry.email_status, Enquiry.EmailStatus.SENT)
        self.assertIsNotNone(enquiry.email_sent_at)
        self.assertEqual(enquiry.email_error, "")
        self.assertFalse(hasattr(enquiry, "website"))

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.from_email, "noreply@phoenixvanz.test")
        self.assertEqual(sent.to, ["enquiries@phoenixvanz.test"])
        self.assertEqual(sent.reply_to, ["jane@example.com"])
        self.assertIn(enquiry.reference, sent.subject)
        self.assertIn("Jane Smith", sent.subject)
        self.assertIn(enquiry.reference, sent.body)
        self.assertIn("Submitted:", sent.body)
        self.assertIn("Jane Smith", sent.body)
        self.assertIn("jane@example.com", sent.body)
        self.assertIn("01234 567890", sent.body)
        self.assertIn("https://phoenixvanz.com/contact", sent.body)
        self.assertIn("Please quote a pop-top conversion.", sent.body)

    def test_invalid_input_returns_400_and_creates_no_row(self):
        response = self.post_enquiry(
            {
                "name": "",
                "email": "not-an-email",
                "message": "",
                "source_url": "not-a-url",
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["success"], False)
        self.assertEqual(response.data["code"], "VALIDATION_ERROR")
        self.assertEqual(
            response.data["error"],
            "Please correct the highlighted fields.",
        )
        self.assertIn("name", response.data["errors"])
        self.assertIn("email", response.data["errors"])
        self.assertIn("message", response.data["errors"])
        self.assertIn("source_url", response.data["errors"])
        self.assertEqual(Enquiry.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_unsupported_field_is_rejected(self):
        payload = {**VALID_ENQUIRY, "company": "Spam Co"}
        response = self.post_enquiry(payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "VALIDATION_ERROR")
        self.assertIn("company", response.data["errors"])
        self.assertEqual(Enquiry.objects.count(), 0)

    def test_populated_honeypot_is_rejected_and_creates_no_row(self):
        payload = {**VALID_ENQUIRY, "website": "http://spam.example"}
        response = self.post_enquiry(payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["success"], False)
        self.assertEqual(response.data["code"], "VALIDATION_ERROR")
        self.assertIn("website", response.data["errors"])
        self.assertEqual(Enquiry.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_send_exception_keeps_enquiry_and_returns_safe_201(self):
        with (
            patch(
                "store.enquiry_email.EmailMessage.send",
                side_effect=OSError("smtp connection refused"),
            ),
            patch("store.views.logger.exception"),
        ):
            response = self.post_enquiry()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["success"], True)
        self.assertEqual(response.data["code"], "ENQUIRY_SAVED_EMAIL_FAILED")
        self.assertEqual(
            response.data["message"],
            "Thanks — your enquiry has been received. We will get back to you shortly.",
        )
        self.assertNotIn("smtp connection refused", str(response.data))
        self.assertNotIn("OSError", str(response.data))

        enquiry = Enquiry.objects.get()
        self.assertEqual(enquiry.reference, response.data["reference"])
        self.assertEqual(enquiry.email_status, Enquiry.EmailStatus.FAILED)
        self.assertIsNone(enquiry.email_sent_at)
        self.assertEqual(enquiry.email_error, "OSError")
        self.assertEqual(len(mail.outbox), 0)

    def test_proxy_endpoint_does_not_require_csrf_or_session(self):
        client = self.client_class(enforce_csrf_checks=True)
        response = client.post(
            self.url,
            VALID_ENQUIRY,
            format="json",
            **self.proxy_headers(),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Enquiry.objects.count(), 1)

    def test_throttling_applies_only_to_the_contact_endpoint(self):
        responses = [self.post_enquiry() for _ in range(6)]
        products = self.client.get(reverse("product-list-api"))

        self.assertTrue(all(item.status_code == 201 for item in responses[:5]))
        limited = responses[5]
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.data["success"], False)
        self.assertEqual(limited.data["code"], "RATE_LIMITED")
        self.assertEqual(
            limited.data["error"],
            "Too many enquiries have been submitted. Please try again later.",
        )
        self.assertEqual(products.status_code, 200)
        self.assertEqual(Enquiry.objects.count(), 5)

    def test_missing_proxy_secret_is_rejected_and_creates_no_row(self):
        response = self.post_enquiry(secret="")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "CONTACT_PROXY_FORBIDDEN")
        self.assertEqual(Enquiry.objects.count(), 0)
        self.assertEqual(ContactRateLimitBucket.objects.count(), 0)

    def test_wrong_proxy_secret_is_rejected_and_creates_no_row(self):
        response = self.post_enquiry(secret="wrong-secret")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "CONTACT_PROXY_FORBIDDEN")
        self.assertEqual(Enquiry.objects.count(), 0)
        self.assertEqual(ContactRateLimitBucket.objects.count(), 0)

    def test_invalid_client_ip_is_rejected(self):
        response = self.post_enquiry(client_ip="not-an-ip")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "CONTACT_PROXY_FORBIDDEN")
        self.assertEqual(Enquiry.objects.count(), 0)
        self.assertEqual(ContactRateLimitBucket.objects.count(), 0)

    def test_correct_secret_and_valid_ip_permit_submission(self):
        response = self.post_enquiry(client_ip="2001:0db8::1")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["code"], "ENQUIRY_RECEIVED")
        bucket = ContactRateLimitBucket.objects.get()
        self.assertEqual(len(bucket.identity_hash), 64)
        self.assertNotIn("2001:db8::1", bucket.identity_hash)

    def test_x_forwarded_for_does_not_affect_throttle_identity(self):
        responses = [
            self.post_enquiry(
                HTTP_X_FORWARDED_FOR=f"198.51.100.{index}"
            )
            for index in range(1, 7)
        ]

        self.assertTrue(all(item.status_code == 201 for item in responses[:5]))
        self.assertEqual(responses[5].status_code, 429)
        self.assertEqual(ContactRateLimitBucket.objects.count(), 1)

    def test_different_validated_ip_has_separate_throttle_bucket(self):
        for _ in range(5):
            self.assertEqual(self.post_enquiry().status_code, 201)

        other = self.post_enquiry(client_ip="203.0.113.11")

        self.assertEqual(other.status_code, 201)
        self.assertEqual(ContactRateLimitBucket.objects.count(), 2)
        self.assertEqual(Enquiry.objects.count(), 6)

    def test_throttle_uses_database_without_locmem_cache(self):
        with (
            patch(
                "django.core.cache.cache.get",
                side_effect=AssertionError("cache.get must not be called"),
            ),
            patch(
                "django.core.cache.cache.set",
                side_effect=AssertionError("cache.set must not be called"),
            ),
        ):
            response = self.post_enquiry()

        self.assertEqual(response.status_code, 201)
        bucket = ContactRateLimitBucket.objects.get()
        self.assertEqual(bucket.request_count, 1)

    def test_authenticated_invalid_submissions_count_toward_throttle(self):
        invalid_payload = {**VALID_ENQUIRY, "email": "invalid"}
        responses = [self.post_enquiry(invalid_payload) for _ in range(6)]

        self.assertTrue(all(item.status_code == 400 for item in responses[:5]))
        self.assertEqual(responses[5].status_code, 429)
        self.assertEqual(Enquiry.objects.count(), 0)

    def test_email_send_returning_zero_records_failed_delivery(self):
        with (
            patch("store.enquiry_email.EmailMessage.send", return_value=0),
            patch("store.views.logger.exception"),
        ):
            response = self.post_enquiry()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["code"], "ENQUIRY_SAVED_EMAIL_FAILED")
        enquiry = Enquiry.objects.get()
        self.assertEqual(enquiry.email_status, Enquiry.EmailStatus.FAILED)
        self.assertEqual(enquiry.email_error, "EmailDeliveryError")

    def test_provider_exception_is_logged_but_stored_diagnostic_is_sanitised(self):
        provider_error = "smtp failed password=super-secret connection=smtp://private"
        with self.assertLogs("store.views", level="ERROR") as captured:
            with patch(
                "store.enquiry_email.EmailMessage.send",
                side_effect=OSError(provider_error),
            ):
                response = self.post_enquiry()

        enquiry = Enquiry.objects.get()
        self.assertEqual(response.data["code"], "ENQUIRY_SAVED_EMAIL_FAILED")
        self.assertTrue(any(provider_error in line for line in captured.output))
        self.assertEqual(enquiry.email_error, "OSError")
        self.assertNotIn("super-secret", enquiry.email_error)
        self.assertNotIn("super-secret", str(response.data))

    def test_sent_status_update_failure_returns_201_and_does_not_resend(self):
        with (
            patch("store.views.Enquiry.objects.filter") as filter_mock,
            patch("store.views.logger.exception") as log_exception,
        ):
            filter_mock.return_value.update.side_effect = DatabaseError(
                "status update failed"
            )
            response = self.post_enquiry()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["code"], "ENQUIRY_RECEIVED")
        self.assertEqual(len(mail.outbox), 1)
        enquiry = Enquiry.objects.get()
        self.assertEqual(enquiry.email_status, Enquiry.EmailStatus.PENDING)
        log_exception.assert_called_once()

    def test_failed_status_update_failure_returns_safe_201(self):
        with (
            patch(
                "store.enquiry_email.EmailMessage.send",
                side_effect=OSError("provider unavailable"),
            ),
            patch("store.views.Enquiry.objects.filter") as filter_mock,
            patch("store.views.logger.exception") as log_exception,
        ):
            filter_mock.return_value.update.side_effect = DatabaseError(
                "status update failed"
            )
            response = self.post_enquiry()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["code"], "ENQUIRY_SAVED_EMAIL_FAILED")
        enquiry = Enquiry.objects.get()
        self.assertEqual(enquiry.email_status, Enquiry.EmailStatus.PENDING)
        self.assertEqual(log_exception.call_count, 2)

    def test_reference_collision_retries_successfully(self):
        collision = "PV-20260928-AAAAAAAAAAAAAAAA"
        replacement = "PV-20260928-BBBBBBBBBBBBBBBB"
        with patch("store.models.generate_enquiry_reference", return_value=collision):
            first = Enquiry.objects.create(
                name="First",
                email="first@example.com",
                message="First enquiry",
            )

        with patch(
            "store.models.generate_enquiry_reference",
            side_effect=[collision, replacement],
        ):
            second = Enquiry.objects.create(
                name="Second",
                email="second@example.com",
                message="Second enquiry",
            )

        self.assertEqual(first.reference, collision)
        self.assertEqual(second.reference, replacement)
        self.assertEqual(Enquiry.objects.count(), 2)

    def test_existing_reference_cannot_be_modified_by_later_save(self):
        enquiry = Enquiry.objects.create(
            name="Jane",
            email="jane@example.com",
            message="Original enquiry",
        )
        original_reference = enquiry.reference

        enquiry.reference = "PV-20260928-CCCCCCCCCCCCCCCC"
        enquiry.name = "Jane Updated"
        enquiry.save()
        enquiry.refresh_from_db()

        self.assertEqual(enquiry.reference, original_reference)
        self.assertEqual(enquiry.name, "Jane Updated")

    def test_name_with_crlf_is_rejected(self):
        response = self.post_enquiry(
            {**VALID_ENQUIRY, "name": "Jane\r\nBcc: attacker@example.com"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "VALIDATION_ERROR")
        self.assertIn("name", response.data["errors"])
        self.assertEqual(Enquiry.objects.count(), 0)

    def test_malformed_json_returns_agreed_validation_envelope(self):
        response = self.client.generic(
            "POST",
            self.url,
            data=b'{"name":',
            content_type="application/json",
            **self.proxy_headers(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {
                "success": False,
                "code": "VALIDATION_ERROR",
                "error": "Please correct the highlighted fields.",
                "errors": {
                    "non_field_errors": [
                        "The request body is not valid JSON."
                    ]
                },
            },
        )

    def test_unsupported_http_method_remains_405(self):
        response = self.client.get(self.url, **self.proxy_headers())

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.data["detail"], 'Method "GET" not allowed.')

    @override_settings(CONTACT_EMAIL_ENABLED=False)
    def test_disabled_email_saves_enquiry_without_calling_delivery(self):
        with patch("store.views.send_enquiry_email") as send_email:
            response = self.post_enquiry()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.data,
            {
                "success": True,
                "code": "ENQUIRY_RECEIVED",
                "message": "Your enquiry has been received.",
                "reference": response.data["reference"],
            },
        )
        self.assertRegex(
            response.data["reference"],
            r"^PV-\d{8}-[A-F0-9]{16}$",
        )
        enquiry = Enquiry.objects.get()
        self.assertEqual(enquiry.reference, response.data["reference"])
        self.assertEqual(enquiry.email_status, Enquiry.EmailStatus.PENDING)
        self.assertIsNone(enquiry.email_sent_at)
        self.assertEqual(enquiry.email_error, "")
        self.assertEqual(len(mail.outbox), 0)
        send_email.assert_not_called()

        browser = Client()
        anonymous = browser.get(reverse("admin:store_enquiry_changelist"))
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/admin/login/", anonymous.url)
        staff = get_user_model().objects.create_superuser(
            "enquiry-admin",
            "enquiry-admin@example.com",
            "enquiry-admin-password",
        )
        staff_request = RequestFactory().get("/admin/store/enquiry/")
        staff_request.user = staff
        model_admin = EnquiryAdmin(Enquiry, admin.site)
        self.assertTrue(model_admin.has_view_permission(staff_request))
        visible = model_admin.get_queryset(staff_request).get()
        self.assertEqual(visible.reference, enquiry.reference)
        self.assertEqual(visible.email_status, Enquiry.EmailStatus.PENDING)
        self.assertIsNone(visible.email_sent_at)

    @override_settings(CONTACT_EMAIL_ENABLED=False)
    def test_disabled_email_rejects_invalid_input_without_success(self):
        with patch("store.views.send_enquiry_email") as send_email:
            response = self.post_enquiry({**VALID_ENQUIRY, "email": "invalid"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["success"], False)
        self.assertEqual(Enquiry.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)
        send_email.assert_not_called()

    @override_settings(CONTACT_EMAIL_ENABLED=False)
    def test_disabled_email_does_not_return_success_when_save_fails(self):
        with (
            patch(
                "store.views.ContactEnquirySerializer.save",
                side_effect=DatabaseError("persistence failed"),
            ),
            patch("store.views.send_enquiry_email") as send_email,
        ):
            with self.assertRaises(DatabaseError):
                self.post_enquiry()

        self.assertEqual(Enquiry.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)
        send_email.assert_not_called()

    def test_enquiry_admin_evidence_is_read_only_and_creation_is_disabled(self):
        model_admin = EnquiryAdmin(Enquiry, admin.site)
        request = RequestFactory().get("/admin/store/enquiry/")
        expected_readonly = {
            "reference",
            "name",
            "email",
            "phone",
            "message",
            "source_url",
            "created_at",
            "email_status",
            "email_sent_at",
            "email_error",
        }

        self.assertEqual(set(model_admin.readonly_fields), expected_readonly)
        self.assertFalse(model_admin.has_add_permission(request))


class ContactProductionConfigurationTests(APITestCase):
    def test_production_requires_contact_proxy_secret(self):
        for secret in ("", "local-development-contact-proxy-secret"):
            with self.subTest(secret=secret):
                with self.assertRaises(ImproperlyConfigured):
                    validate_contact_proxy_secret(debug=False, secret=secret)

    def test_production_rejects_missing_email_backend(self):
        configuration = {
            **PRODUCTION_EMAIL_SETTINGS,
            "email_backend": "",
        }
        with self.assertRaises(ImproperlyConfigured):
            validate_production_email_configuration(**configuration)

    def test_production_rejects_non_delivery_email_backends(self):
        backends = [
            "django.core.mail.backends.console.EmailBackend",
            "django.core.mail.backends.locmem.EmailBackend",
            "django.core.mail.backends.dummy.EmailBackend",
            "django.core.mail.backends.filebased.EmailBackend",
        ]
        for backend in backends:
            with self.subTest(backend=backend):
                configuration = {
                    **PRODUCTION_EMAIL_SETTINGS,
                    "email_backend": backend,
                }
                with self.assertRaises(ImproperlyConfigured):
                    validate_production_email_configuration(**configuration)

    def test_production_rejects_missing_or_invalid_email_addresses(self):
        cases = [
            ("default_from_email", ""),
            ("default_from_email", "not-an-email"),
            ("contact_recipient_email", ""),
            ("contact_recipient_email", "not-an-email"),
        ]
        for setting_name, value in cases:
            with self.subTest(setting_name=setting_name, value=value):
                configuration = {
                    **PRODUCTION_EMAIL_SETTINGS,
                    setting_name: value,
                }
                with self.assertRaises(ImproperlyConfigured):
                    validate_production_email_configuration(**configuration)

    def test_production_rejects_tls_and_ssl_together(self):
        configuration = {
            **PRODUCTION_EMAIL_SETTINGS,
            "email_use_ssl": True,
        }
        with self.assertRaises(ImproperlyConfigured):
            validate_production_email_configuration(**configuration)

    def test_production_rejects_incomplete_smtp_settings(self):
        for setting_name, value in (
            ("email_host", ""),
            ("email_port", 0),
            ("email_host_user", ""),
            ("email_host_password", ""),
        ):
            with self.subTest(setting_name=setting_name):
                configuration = {
                    **PRODUCTION_EMAIL_SETTINGS,
                    setting_name: value,
                }
                with self.assertRaises(ImproperlyConfigured):
                    validate_production_email_configuration(**configuration)

    def test_boolean_setting_accepts_only_real_booleans(self):
        for value in (True, "true", "True", "1", "yes", "on"):
            with self.subTest(value=value):
                self.assertIs(
                    parse_boolean_setting(value, setting_name="CONTACT_EMAIL_ENABLED"),
                    True,
                )
        for value in (False, "false", "False", "0", "no", "off"):
            with self.subTest(value=value):
                self.assertIs(
                    parse_boolean_setting(value, setting_name="CONTACT_EMAIL_ENABLED"),
                    False,
                )
        with self.assertRaises(ImproperlyConfigured):
            parse_boolean_setting("maybe", setting_name="CONTACT_EMAIL_ENABLED")

    def test_disabled_email_skips_only_the_email_configuration_requirement(self):
        validate_production_email_configuration(
            debug=False,
            email_backend="",
            email_host="",
            email_port=0,
            email_use_tls=False,
            email_use_ssl=False,
            email_host_user="",
            email_host_password="",
            default_from_email="",
            contact_recipient_email="",
            contact_email_enabled=False,
        )
        with self.assertRaises(ImproperlyConfigured):
            validate_contact_proxy_secret(debug=False, secret="")

    def test_production_boots_with_email_disabled_and_no_email_configuration(self):
        completed = _boot_contact_settings(
            {
                "CONTACT_EMAIL_ENABLED": "false",
                "CONTACT_PROXY_SECRET": "contact-save-only-proxy-not-production",
            }
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("email_enabled=False", completed.stdout)
        self.assertIn("booted", completed.stdout)

    def test_disabled_email_still_requires_the_contact_proxy_secret(self):
        completed = _boot_contact_settings({"CONTACT_EMAIL_ENABLED": "false"})
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("CONTACT_PROXY_SECRET", completed.stderr)
        self.assertNotIn("EMAIL_BACKEND", completed.stderr)


def _boot_contact_settings(extra):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "DJANGO_SETTINGS_MODULE": "base.settings",
        "DEBUG_SETTING": "False",
        "DJANGO_SECRET_KEY": "contact-save-only-test-not-a-production-secret",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_SHARED_CREDENTIALS_FILE": "/aws-missing/credentials",
        "AWS_CONFIG_FILE": "/aws-missing/config",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra)
    script = textwrap.dedent(
        """
        import django
        from django.conf import settings

        settings.DATABASES["default"] = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
        django.setup()
        print("email_enabled=" + str(settings.CONTACT_EMAIL_ENABLED))
        print("booted")
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
