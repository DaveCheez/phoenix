"""Application credential and shopper-address guard.

Accepting an address from a valid application credential does not prove that
the address is the shopper's true network address.
"""

import logging
import sys
from unittest.mock import patch

from django.core import mail
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.views.debug import get_exception_reporter_class
from rest_framework.test import APIClient, APITestCase

from .application_access import ApplicationAccessRejected, canonical_shopper_address
from .models import Cart, CartItem, GuestSession
from .test_application_client import (
    TEST_APP_CREDENTIAL,
    TEST_SHOPPER_ADDRESS,
    cart_app_settings,
)


_REJECTED = {
    "success": False,
    "code": "CART_APPLICATION_REJECTED",
    "error": "Cart service is temporarily unavailable.",
}


def _headers(**extra):
    headers = {
        "HTTP_X_PHOENIX_APP_CREDENTIAL": TEST_APP_CREDENTIAL,
        "HTTP_X_PHOENIX_SHOPPER_ADDRESS": TEST_SHOPPER_ADDRESS,
    }
    headers.update(extra)
    return headers


class ShopperAddressSyntaxTests(SimpleTestCase):
    def test_ipv4_ipv6_and_mapped_ipv6_are_canonical(self):
        self.assertEqual(canonical_shopper_address("203.0.113.10"), "203.0.113.10")
        self.assertEqual(canonical_shopper_address("2001:DB8::1"), "2001:db8::1")
        self.assertEqual(canonical_shopper_address("::ffff:203.0.113.9"), "203.0.113.9")
        self.assertEqual(
            canonical_shopper_address("0:0:0:0:0:ffff:203.0.113.9"),
            "203.0.113.9",
        )
        self.assertEqual(canonical_shopper_address("127.0.0.1"), "127.0.0.1")

    def test_malformed_addresses_are_rejected(self):
        rejected = [
            "",
            " 203.0.113.10",
            "203.0.113.10 ",
            "203.0.113.10,198.51.100.1",
            "203.0.113.10:80",
            "[2001:db8::1]",
            "203.0.113.0/24",
            "fe80::1%eth0",
            "http://203.0.113.10",
            "203.0.113.10\n",
            "203.0.113.1\u3000",
            "x" * 46,
            b"203.0.113.10",
            None,
        ]
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(ApplicationAccessRejected):
                    canonical_shopper_address(value)


@cart_app_settings
class CartApplicationGuardTests(APITestCase):
    def assert_rejected(self, response):
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data, _REJECTED)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn("WWW-Authenticate", response)
        self.assertEqual(list(response.cookies.keys()), [])

    def test_anonymous_start_requires_application_headers_not_a_guest_bearer(self):
        response = APIClient().post(
            reverse("create_cart"),
            {"action": "start"},
            format="json",
            **_headers(),
        )

        self.assertEqual(response.status_code, 201)
        self.assertIn("guest_access", response.data)
        self.assertEqual(GuestSession.objects.count(), 1)
        self.assertEqual(response.data["guest_access"]["token"].__class__, str)

    def test_guest_bearer_alone_cannot_open_a_cart(self):
        from .guest_access import issue_guest_cart

        issued = issue_guest_cart()
        response = APIClient().get(
            reverse("get_cart"),
            HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
        )

        self.assert_rejected(response)
        self.assertEqual(Cart.objects.count(), 1)

    def test_bad_application_credentials_are_rejected_before_guest_work(self):
        before = GuestSession.objects.count()
        cases = [
            {},
            {"HTTP_X_PHOENIX_APP_CREDENTIAL": ""},
            {"HTTP_X_PHOENIX_APP_CREDENTIAL": "not-hex"},
            {"HTTP_X_PHOENIX_APP_CREDENTIAL": TEST_APP_CREDENTIAL.upper()},
            {"HTTP_X_PHOENIX_APP_CREDENTIAL": " " + TEST_APP_CREDENTIAL},
            {"HTTP_X_PHOENIX_APP_CREDENTIAL": "ab" * 32},
            {
                "HTTP_X_PHOENIX_APP_CREDENTIAL": (
                    f"{TEST_APP_CREDENTIAL}, {TEST_APP_CREDENTIAL}"
                )
            },
        ]
        for extra in cases:
            response = APIClient().post(
                reverse("create_cart"),
                {"action": "start", "product_id": 999999},
                format="json",
                HTTP_X_PHOENIX_SHOPPER_ADDRESS=TEST_SHOPPER_ADDRESS,
                **extra,
            )
            self.assert_rejected(response)
        self.assertEqual(GuestSession.objects.count(), before)
        self.assertFalse(CartItem.objects.exists())

    def test_valid_application_and_invalid_guest_bearer_stays_a_guest_401(self):
        response = APIClient().get(
            reverse("get_cart"),
            HTTP_AUTHORIZATION="Bearer not-a-token",
            **_headers(),
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["code"], "CART_ACCESS_UNAVAILABLE")
        self.assertEqual(response["WWW-Authenticate"], 'Bearer realm="cart"')

    def test_bad_shopper_addresses_are_rejected(self):
        before = GuestSession.objects.count()
        for address in ("", "203.0.113.10, 198.51.100.1", "203.0.113.10:443", None):
            extra = {}
            if address is not None:
                extra["HTTP_X_PHOENIX_SHOPPER_ADDRESS"] = address
            response = APIClient().post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
                HTTP_X_PHOENIX_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
                **extra,
            )
            self.assert_rejected(response)
        self.assertEqual(GuestSession.objects.count(), before)

    def test_forwarded_and_body_fields_do_not_supply_the_address(self):
        response = APIClient().post(
            reverse("create_cart"),
            {
                "action": "start",
                "shopper_address": TEST_SHOPPER_ADDRESS,
                "client_ip": TEST_SHOPPER_ADDRESS,
            },
            format="json",
            HTTP_X_PHOENIX_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
            HTTP_X_FORWARDED_FOR=TEST_SHOPPER_ADDRESS,
            HTTP_DO_CONNECTING_IP=TEST_SHOPPER_ADDRESS,
        )

        self.assert_rejected(response)
        self.assertFalse(GuestSession.objects.exists())

    def test_canonical_addresses_are_accepted_for_start(self):
        for address in ("2001:DB8::1", "::ffff:203.0.113.50", "127.0.0.1"):
            response = APIClient().post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
                HTTP_X_PHOENIX_APP_CREDENTIAL=TEST_APP_CREDENTIAL,
                HTTP_X_PHOENIX_SHOPPER_ADDRESS=address,
            )
            self.assertEqual(response.status_code, 201, address)
        self.assertEqual(GuestSession.objects.count(), 3)

    def test_unconfigured_and_placeholder_credentials_fail_closed(self):
        presented = _headers()
        for value in ("", "replace-with-64-lowercase-hex-chars", "A" * 64, "0" * 64):
            with override_settings(CART_APP_CREDENTIAL=value):
                response = APIClient().post(
                    reverse("create_cart"),
                    {"action": "start"},
                    format="json",
                    **presented,
                )
            self.assert_rejected(response)
        self.assertFalse(GuestSession.objects.exists())

    def test_bad_credential_does_not_reveal_a_missing_product(self):
        response = APIClient().post(
            reverse("add_to_cart"),
            {"product_id": 999999, "quantity": 1},
            format="json",
            HTTP_X_PHOENIX_APP_CREDENTIAL="ab" * 32,
            HTTP_X_PHOENIX_SHOPPER_ADDRESS=TEST_SHOPPER_ADDRESS,
        )

        self.assert_rejected(response)
        self.assertNotEqual(response.data["code"], "PRODUCT_NOT_FOUND")
        self.assertFalse(Cart.objects.exists())

    def test_options_and_unsupported_methods_stay_safe_without_application_headers(self):
        options = APIClient().options(reverse("create_cart"))
        rejected = APIClient().put(reverse("add_to_cart"), {}, format="json")

        self.assertEqual(options.status_code, 200)
        self.assertNotIn("guest_access", options.content.decode())
        self.assertEqual(rejected.status_code, 405)
        self.assertFalse(GuestSession.objects.exists())

    def test_health_does_not_require_the_cart_application_credential(self):
        with override_settings(CART_APP_CREDENTIAL=""):
            health = APIClient().get("/health/")
            cart = APIClient().post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
                **_headers(),
            )

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assert_rejected(cart)


_REPORT_CREDENTIAL = "fedcba9876543210" * 4
_REPORT_ADDRESS = "198.51.100.23"


@override_settings(
    DEBUG=False,
    ADMINS=[("Ops", "ops@example.com")],
    CART_APP_CREDENTIAL=_REPORT_CREDENTIAL,
)
class CartApplicationReportTests(APITestCase):
    def test_production_reports_redact_application_credential_and_address(self):
        from django.test import RequestFactory

        request = RequestFactory().get(
            "/api/cart/",
            HTTP_X_PHOENIX_APP_CREDENTIAL=_REPORT_CREDENTIAL,
            HTTP_X_PHOENIX_SHOPPER_ADDRESS=_REPORT_ADDRESS,
        )
        try:
            raise RuntimeError("application report")
        except RuntimeError:
            reporter = get_exception_reporter_class(request)(request, *sys.exc_info())
        text = reporter.get_traceback_text()
        self.assertNotIn(_REPORT_CREDENTIAL, text)
        self.assertNotIn(_REPORT_ADDRESS, text)
        self.assertEqual(
            reporter.get_traceback_data()["settings"]["CART_APP_CREDENTIAL"],
            reporter.filter.cleansed_substitute,
        )
        meta = reporter.get_traceback_data()["request_meta"]
        self.assertEqual(
            meta["HTTP_X_PHOENIX_APP_CREDENTIAL"],
            reporter.filter.cleansed_substitute,
        )
        self.assertEqual(
            meta["HTTP_X_PHOENIX_SHOPPER_ADDRESS"],
            reporter.filter.cleansed_substitute,
        )

    def test_live_request_report_omits_the_new_secrets(self):
        from .guest_access import issue_guest_cart

        issued = issue_guest_cart()
        mail.outbox.clear()
        self.client.raise_request_exception = False
        request_logger = logging.getLogger("django.request")
        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        handler = _Capture()
        request_logger.addHandler(handler)
        try:
            with patch("cart.views.cart_payload", side_effect=RuntimeError("application report")):
                response = self.client.get(
                    reverse("get_cart"),
                    HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
                    HTTP_X_PHOENIX_APP_CREDENTIAL=_REPORT_CREDENTIAL,
                    HTTP_X_PHOENIX_SHOPPER_ADDRESS=_REPORT_ADDRESS,
                )
        finally:
            request_logger.removeHandler(handler)

        self.assertEqual(response.status_code, 500)
        outputs = [
            response.content.decode(),
            "\n".join(captured),
            mail.outbox[0].body,
        ]
        for output in outputs:
            self.assertNotIn(_REPORT_CREDENTIAL, output)
            self.assertNotIn(_REPORT_ADDRESS, output)
            self.assertNotIn(issued.raw_token, output)
        self.assertIn("HTTP_X_PHOENIX_APP_CREDENTIAL", mail.outbox[0].body)
        self.assertIn("HTTP_X_PHOENIX_SHOPPER_ADDRESS", mail.outbox[0].body)
        self.assertIn("********************", mail.outbox[0].body)
        self.assertEqual(response["Cache-Control"], "no-store")
