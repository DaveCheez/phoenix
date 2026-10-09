"""HTTP enforcement for guest-session cart routes.

SQLite checks the responses. It does not prove PostgreSQL row locks.
The concurrency class at the bottom runs only on PostgreSQL.
"""

import hashlib
import logging
import sys
import threading
import time
import unittest
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.db import InterfaceError, OperationalError, connection, transaction
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.views.debug import get_exception_reporter_class
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APITestCase, APITransactionTestCase
from .test_application_client import CartAPIClient, cart_app_settings
from unittest.mock import patch

from store.models import Category, Product, ProductOption, ProductOptionGroup

from .guest_access import issue_guest_cart
from .models import Cart, CartItem, GuestSession


_ACCESS = {
    "success": False,
    "code": "CART_ACCESS_UNAVAILABLE",
    "error": "Cart access is not available.",
}


def _product(name="Clip", slug="http-clip", price="10.00"):
    category = Category.objects.create(name=f"{name} category", slug=f"{slug}-cat", type="product")
    return Product.objects.create(
        category=category,
        name=name,
        slug=slug,
        price=Decimal(price),
    )


@cart_app_settings
class CartHttpContractTests(APITestCase):
    client_class = CartAPIClient

    def assert_denied(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(response.data, _ACCESS)
        self.assertEqual(response["WWW-Authenticate"], 'Bearer realm="cart"')
        self.assertEqual(response["Cache-Control"], "no-store")

    def _auth(self, issued):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}")

    def test_explicit_start_issues_one_credential_and_cart(self):
        response = self.client.post(reverse("create_cart"), {"action": "start"}, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn("cart_id", response.cookies)
        self.assertNotIn("sessionid", response.cookies)
        self.assertEqual(GuestSession.objects.count(), 1)
        self.assertEqual(Cart.objects.count(), 1)
        session = GuestSession.objects.get()
        self.assertEqual(response.data["guest_access"]["expires_at"], session.expires_at.isoformat())
        self.assertEqual(response.data["guest_access"]["token"] != session.token_hash, True)
        self.assertNotIn(response.data["guest_access"]["token"], session.token_hash)
        self.assertEqual(response.data["cart"]["items"], [])
        self.assertEqual(str(Cart.objects.get().guest_session_id), str(session.id))
        self.assertIsNone(Cart.objects.get().session_key)

    def test_no_action_bad_bearer_and_issuance_failure_create_nothing(self):
        missing = self.client.post(reverse("create_cart"), {}, format="json")
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.data["code"], "START_REQUIRED")
        self.assertEqual(missing["Cache-Control"], "no-store")

        invalid = self.client.post(
            reverse("create_cart"),
            {"action": "start"},
            format="json",
            HTTP_AUTHORIZATION="Bearer not-a-token",
        )
        self.assert_denied(invalid)

        with patch("cart.views.issue_guest_cart", side_effect=OperationalError("down")):
            unavailable = self.client.post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
        self.assertNotIn("guest_access", unavailable.data)

        with patch("cart.views.issue_guest_cart", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("create_cart"), {"action": "start"}, format="json")

        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(Cart.objects.exists())

    def test_start_does_not_claim_a_legacy_cart(self):
        legacy = Cart.objects.create(session_key="browser-session")

        response = self.client.post(
            reverse("create_cart"),
            {"action": "start", "cart_id": str(legacy.id)},
            format="json",
        )

        self.assert_denied(response)
        legacy.refresh_from_db()
        self.assertIsNone(legacy.guest_session_id)
        self.assertFalse(GuestSession.objects.exists())

    def test_repeat_start_does_not_rotate_or_extend_the_session(self):
        issued = issue_guest_cart()
        original_expiry = issued.cart.guest_session.expires_at
        original_hash = issued.cart.guest_session.token_hash
        self._auth(issued)

        response = self.client.post(reverse("create_cart"), {"action": "start"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("guest_access", response.data)
        self.assertNotIn(issued.raw_token, str(response.data))
        self.assertEqual(GuestSession.objects.count(), 1)
        session = GuestSession.objects.get()
        self.assertEqual(session.expires_at, original_expiry)
        self.assertEqual(session.token_hash, original_hash)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_supported_methods_can_read_and_change_only_their_cart(self):
        issued = issue_guest_cart()
        product = _product()
        self._auth(issued)
        add = self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 1, "options": []},
            format="json",
        )
        self.assertEqual(add.status_code, 201)
        self.assertEqual(add.data["cart"]["items"][0]["configured_unit_price"], "10.00")
        item_id = add.data["cart"]["items"][0]["item_id"]

        fetched = self.client.get(reverse("get_cart"))
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.data["cart"]["item_count"], 1)

        posted = self.client.post(
            reverse("update_cart_item"),
            {"item_id": item_id, "quantity": 2},
            format="json",
        )
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(posted.data["cart"]["items"][0]["quantity"], 2)

        patched = self.client.patch(
            reverse("update_cart_item"),
            {"item_id": item_id, "quantity": 3},
            format="json",
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.data["cart"]["total"], "30.00")

        removed = self.client.post(
            reverse("remove_from_cart"),
            {"item_id": item_id},
            format="json",
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.data["cart"]["items"], [])

        self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 1, "options": []},
            format="json",
        )
        item_id = CartItem.objects.get().id
        deleted = self.client.delete(
            reverse("remove_from_cart"),
            {"item_id": item_id},
            format="json",
        )
        self.assertEqual(deleted.status_code, 200)

        self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 1, "options": []},
            format="json",
        )
        cleared = self.client.post(reverse("clear_cart"), {}, format="json")
        self.assertEqual(cleared.status_code, 200)
        self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 1, "options": []},
            format="json",
        )
        cleared = self.client.delete(reverse("clear_cart"), {}, format="json")
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.data["cart"]["items"], [])
        self.assertTrue(Cart.objects.filter(pk=issued.cart.id).exists())
        self.assertTrue(GuestSession.objects.filter(pk=issued.cart.guest_session_id).exists())
        self.assertFalse(CartItem.objects.exists())

    def test_scheme_case_is_ignored_and_token_case_is_not(self):
        issued = issue_guest_cart()
        for scheme in ("Bearer", "bearer", "BEARER", "bEaReR"):
            response = self.client.get(
                reverse("get_cart"),
                HTTP_AUTHORIZATION=f"{scheme} {issued.raw_token}",
            )
            self.assertEqual(response.status_code, 200, scheme)

        altered = issued.raw_token.swapcase()
        if altered != issued.raw_token:
            denied = self.client.get(
                reverse("get_cart"),
                HTTP_AUTHORIZATION=f"Bearer {altered}",
            )
            self.assert_denied(denied)

    def test_access_failures_share_one_response(self):
        issued = issue_guest_cart()
        product = _product(slug="hidden-product")
        other = issue_guest_cart()
        legacy = Cart.objects.create(session_key="legacy-browser")
        session = issued.cart.guest_session
        cases = [
            {},
            {"HTTP_AUTHORIZATION": ""},
            {"HTTP_AUTHORIZATION": "Bearer"},
            {"HTTP_AUTHORIZATION": f"Bearer {issued.raw_token} "},
            {"HTTP_AUTHORIZATION": f"Bearer\t{issued.raw_token}"},
            {"HTTP_AUTHORIZATION": f'Bearer "{issued.raw_token}"'},
            {"HTTP_AUTHORIZATION": f"Bearer {issued.raw_token}, Bearer {other.raw_token}"},
            {"HTTP_AUTHORIZATION": f"Bearer {issued.cart.id}"},
            {"HTTP_AUTHORIZATION": f"Bearer {session.id}"},
            {"HTTP_AUTHORIZATION": "Bearer browser-session"},
            {"HTTP_COOKIE": f"cart_id={issued.cart.id}"},
        ]
        for extra in cases:
            response = self.client.get(reverse("get_cart"), **extra)
            self.assert_denied(response)

        session.expires_at = timezone.now() - timedelta(seconds=1)
        session.save(update_fields=["expires_at"])
        expired = self.client.get(
            reverse("get_cart"),
            HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
        )
        self.assert_denied(expired)

        session.expires_at = timezone.now() + timedelta(days=1)
        session.save(update_fields=["expires_at"])
        with patch("cart.guest_access.timezone.now", return_value=session.expires_at):
            exact = self.client.get(
                reverse("get_cart"),
                HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
            )
        self.assert_denied(exact)

        session.revoked_at = timezone.now()
        session.expires_at = timezone.now() + timedelta(days=1)
        session.save(update_fields=["revoked_at", "expires_at"])
        revoked = self.client.get(
            reverse("get_cart"),
            HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}",
        )
        self.assert_denied(revoked)

        wrong = self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 1, "options": []},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {other.raw_token}",
        )
        self.assertEqual(wrong.status_code, 201)
        self.assertEqual(wrong.data["cart"]["id"], str(other.cart.id))
        self.assertFalse(CartItem.objects.filter(cart=issued.cart).exists())

        user = User.objects.create_user("shopper", "shopper@example.com", "password")
        self.client.force_login(user)
        self.client.credentials()
        logged_in = self.client.get(reverse("get_cart"))
        self.assert_denied(logged_in)
        legacy.refresh_from_db()
        self.assertIsNone(legacy.guest_session_id)

    def test_empty_or_ambiguous_header_cannot_start(self):
        before = GuestSession.objects.count()
        for header in ("", "Bearer", "Bearer token, Bearer other", 'Bearer "quoted-token-value-not-accepted-here"'):
            response = self.client.post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
                HTTP_AUTHORIZATION=header,
            )
            self.assert_denied(response)
        self.assertEqual(GuestSession.objects.count(), before)

    def test_mismatched_cart_id_does_not_change_either_cart(self):
        issued = issue_guest_cart()
        other = issue_guest_cart()
        product = _product(slug="stable-clip")
        self._auth(issued)
        added = self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 1, "options": []},
            format="json",
        )
        self.assertEqual(added.status_code, 201)

        denied = self.client.post(
            reverse("add_to_cart") + f"?cart_id={other.cart.id}",
            {
                "cart_id": str(issued.cart.id),
                "product_id": product.id,
                "quantity": 1,
                "options": [],
            },
            format="json",
        )
        self.assert_denied(denied)
        self.assertEqual(CartItem.objects.get().quantity, 1)
        self.assertFalse(CartItem.objects.filter(cart=other.cart).exists())

        conflict = self.client.get(
            reverse("get_cart") + f"?cart_id={issued.cart.id}&cart_id={other.cart.id}"
        )
        self.assert_denied(conflict)
        self.assertEqual(CartItem.objects.get().quantity, 1)

    def test_another_guest_cannot_change_an_item(self):
        issued = issue_guest_cart()
        other = issue_guest_cart()
        product = _product(slug="owned-clip")
        self._auth(issued)
        added = self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 2, "options": []},
            format="json",
        )
        item_id = added.data["cart"]["items"][0]["item_id"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other.raw_token}")

        response = self.client.patch(
            reverse("update_cart_item"),
            {"item_id": item_id, "quantity": 9},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "ITEM_NOT_FOUND")
        self.assertEqual(CartItem.objects.get().quantity, 2)

    def test_invalid_bearer_does_not_reveal_product_or_item_existence(self):
        product = _product(slug="secret-product")
        missing_product = self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id + 1000, "quantity": 1},
            format="json",
            HTTP_AUTHORIZATION="Bearer not-a-token",
        )
        self.assert_denied(missing_product)

        missing_item = self.client.patch(
            reverse("update_cart_item"),
            {"item_id": 999999, "quantity": 1},
            format="json",
            HTTP_AUTHORIZATION="",
        )
        self.assert_denied(missing_item)

    def test_missing_cart_is_a_conflict_until_replace_missing(self):
        issued = issue_guest_cart()
        product = _product(slug="replace-clip")
        session_id = issued.cart.guest_session_id
        expiry = issued.cart.guest_session.expires_at
        token_hash = issued.cart.guest_session.token_hash
        issued.cart.delete()
        self._auth(issued)

        fetched = self.client.get(reverse("get_cart"))
        self.assertEqual(fetched.status_code, 409)
        self.assertEqual(fetched.data["code"], "GUEST_CART_MISSING")
        self.assertEqual(fetched["Cache-Control"], "no-store")
        self.assertFalse(Cart.objects.exists())

        anonymous = CartAPIClient()
        rejected = anonymous.post(
            reverse("create_cart"),
            {"action": "replace_missing"},
            format="json",
        )
        self.assert_denied(rejected)
        self.assertFalse(Cart.objects.exists())

        created = self.client.post(
            reverse("create_cart"),
            {"action": "replace_missing"},
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        self.assertNotIn("guest_access", created.data)
        self.assertEqual(created.data["cart"]["items"], [])
        self.assertEqual(Cart.objects.count(), 1)
        cart = Cart.objects.get()
        self.assertEqual(cart.guest_session_id, session_id)
        session = GuestSession.objects.get()
        self.assertEqual(session.expires_at, expiry)
        self.assertEqual(session.token_hash, token_hash)
        self.assertIsNone(session.revoked_at)

        self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 2, "options": []},
            format="json",
        )
        again = self.client.post(
            reverse("create_cart"),
            {"action": "replace_missing"},
            format="json",
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data["cart"]["item_count"], 2)
        self.assertEqual(Cart.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 2)

    def test_replace_missing_rejects_a_supplied_cart_id(self):
        issued = issue_guest_cart()
        product = _product(slug="replace-guard")
        session = issued.cart.guest_session
        expiry = session.expires_at
        token_hash = session.token_hash
        self._auth(issued)
        added = self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 2, "options": []},
            format="json",
        )
        self.assertEqual(added.status_code, 201)
        cart_id = str(issued.cart.id)

        body = self.client.post(
            reverse("create_cart"),
            {"action": "replace_missing", "cart_id": cart_id},
            format="json",
        )
        query = self.client.post(
            reverse("create_cart") + f"?cart_id={cart_id}",
            {"action": "replace_missing"},
            format="json",
        )
        self.assert_denied(body)
        self.assert_denied(query)
        session.refresh_from_db()
        self.assertEqual(session.expires_at, expiry)
        self.assertEqual(session.token_hash, token_hash)
        self.assertEqual(Cart.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 2)

        issued.cart.delete()
        missing_body = self.client.post(
            reverse("create_cart"),
            {"action": "replace_missing", "cart_id": cart_id},
            format="json",
        )
        missing_query = self.client.post(
            reverse("create_cart") + f"?cart_id={cart_id}",
            {"action": "replace_missing"},
            format="json",
        )
        self.assert_denied(missing_body)
        self.assert_denied(missing_query)
        self.assertFalse(Cart.objects.exists())
        session.refresh_from_db()
        self.assertEqual(session.expires_at, expiry)
        self.assertEqual(session.token_hash, token_hash)
        self.assertIsNone(session.revoked_at)

    def test_access_failure_rolls_back_a_partial_write(self):
        issued = issue_guest_cart()
        product = _product(slug="rollback-clip")
        self._auth(issued)

        def fail(**kwargs):
            CartItem.objects.create(
                cart=kwargs["cart"],
                product=product,
                quantity=1,
                configuration_signature="",
            )
            raise OperationalError("down")

        with patch("cart.views.add_product_to_cart", side_effect=fail):
            response = self.client.post(
                reverse("add_to_cart"),
                {"product_id": product.id, "quantity": 1, "options": []},
                format="json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(CartItem.objects.exists())

    def test_ordinary_responses_omit_the_credential(self):
        issued = issue_guest_cart()
        self._auth(issued)
        response = self.client.get(reverse("get_cart"), {"cart_id": str(issued.cart.id)})
        encoded = str(response.data)
        self.assertNotIn(issued.raw_token, encoded)
        self.assertNotIn(issued.cart.guest_session.token_hash, encoded)
        self.assertNotIn("expires_at", encoded)
        self.assertNotIn("revoked_at", encoded)
        self.assertNotIn("guest_access", response.data)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_options_is_safe_and_unsupported_methods_are_rejected(self):
        options = self.client.options(reverse("create_cart"))
        self.assertEqual(options.status_code, 200)
        self.assertEqual(options["Cache-Control"], "no-store")
        self.assertNotIn("guest_access", options.content.decode())
        self.assertFalse(GuestSession.objects.exists())

        rejected = self.client.put(reverse("add_to_cart"), {}, format="json")
        self.assertEqual(rejected.status_code, 405)
        self.assertEqual(rejected["Cache-Control"], "no-store")
        self.assertFalse(GuestSession.objects.exists())

    def test_configured_option_line_keeps_its_price(self):
        issued = issue_guest_cart()
        product = _product(name="Rack", slug="http-rack", price="100.00")
        group = ProductOptionGroup.objects.create(product=product, name="Finish", required=True)
        option = ProductOption.objects.create(
            group=group,
            name="Powder",
            price_adjustment=Decimal("12.50"),
        )
        self._auth(issued)

        first = self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 1, "options": [option.id]},
            format="json",
        )
        second = self.client.post(
            reverse("add_to_cart"),
            {"product_id": product.id, "quantity": 1, "options": [option.id]},
            format="json",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(CartItem.objects.count(), 1)
        item = second.data["cart"]["items"][0]
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(item["configured_unit_price"], "112.50")
        self.assertEqual(item["selected_options"][0]["option_id"], option.id)
        self.assertTrue(item["configuration_valid"])


@override_settings(
    DEBUG=False,
    ADMINS=[("Ops", "ops@example.com")],
    CART_APP_CREDENTIAL="0123456789abcdef" * 4,
)
class CartCredentialReportTests(APITestCase):
    client_class = CartAPIClient
    def test_production_reports_redact_the_credential(self):
        issued = issue_guest_cart()
        token = issued.raw_token
        token_hash = issued.cart.guest_session.token_hash
        request = RequestFactory().get(
            "/api/cart/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        request.COOKIES["cart_id"] = token
        try:
            raise RuntimeError("cart report")
        except RuntimeError:
            reporter = get_exception_reporter_class(request)(request, *sys.exc_info())
        report = reporter.get_traceback_text()
        data = reporter.get_traceback_data()
        self.assertNotIn(token, report)
        self.assertNotIn(token_hash, report)
        self.assertEqual(
            data["request_meta"]["HTTP_AUTHORIZATION"],
            reporter.filter.cleansed_substitute,
        )
        self.assertEqual(
            dict(data["request_COOKIES_items"])["cart_id"],
            reporter.filter.cleansed_substitute,
        )

        mail.outbox.clear()
        self.client.raise_request_exception = False
        self.client.cookies["cart_id"] = token
        request_logger = logging.getLogger("django.request")
        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        handler = _Capture()
        request_logger.addHandler(handler)
        try:
            with patch("cart.views.cart_payload", side_effect=RuntimeError("cart report")):
                response = self.client.get(
                    reverse("get_cart"),
                    HTTP_AUTHORIZATION=f"Bearer {token}",
                )
        finally:
            request_logger.removeHandler(handler)
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertNotIn(token, body)
        self.assertNotIn(token_hash, body)
        self.assertEqual(response["Cache-Control"], "no-store")
        logged = "\n".join(captured)
        self.assertNotIn(token, logged)
        self.assertNotIn(token_hash, logged)
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0].body
        self.assertNotIn(token, email)
        self.assertNotIn(token_hash, email)
        self.assertIn("HTTP_AUTHORIZATION", email)


@cart_app_settings
class CartIssuanceTransactionTests(APITransactionTestCase):
    client_class = CartAPIClient
    """Commit-boundary coverage. TestCase would hide a real commit."""

    def test_start_serializes_inside_the_issuance_transaction(self):
        seen = {}
        original = JSONRenderer.render

        def spy(renderer, data, accepted_media_type=None, renderer_context=None):
            if not renderer_context:
                return original(renderer, data, accepted_media_type, renderer_context)
            seen["calls"] = seen.get("calls", 0) + 1
            seen["inside_transaction"] = transaction.get_connection().in_atomic_block
            seen["sessions_during_render"] = GuestSession.objects.count()
            seen["rendered_guest_access"] = (
                isinstance(data, dict) and "guest_access" in data and "cart" in data
            )
            return original(renderer, data, accepted_media_type, renderer_context)

        with patch.object(JSONRenderer, "render", spy):
            response = self.client.post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )

        self.assertEqual(seen["calls"], 1)
        self.assertTrue(seen["inside_transaction"])
        self.assertEqual(seen["sessions_during_render"], 1)
        self.assertTrue(seen["rendered_guest_access"])
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.is_rendered)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn("cart_id", response.cookies)
        self.assertNotIn("sessionid", response.cookies)
        self.assertEqual(GuestSession.objects.count(), 1)
        self.assertEqual(Cart.objects.count(), 1)
        session = GuestSession.objects.get()
        self.assertEqual(response.data["guest_access"]["expires_at"], session.expires_at.isoformat())
        self.assertEqual(str(Cart.objects.get().guest_session_id), str(session.id))
        self.assertNotIn(response.data["guest_access"]["token"], session.token_hash)

    def test_payload_operational_error_rolls_back_real_issuance(self):
        keeper = issue_guest_cart()
        keeper_expiry = keeper.cart.guest_session.expires_at
        keeper_hash = keeper.cart.guest_session.token_hash

        def fail(cart):
            self.assertEqual(GuestSession.objects.count(), 2)
            self.assertNotEqual(cart.id, keeper.cart.id)
            self.assertIsNotNone(cart.guest_session_id)
            raise OperationalError("down")

        with patch("cart.views.cart_payload", side_effect=fail):
            response = self.client.post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
        self.assertNotIn("guest_access", response.data)
        self.assertNotIn("token", response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn("cart_id", response.cookies)
        self.assertEqual(GuestSession.objects.count(), 1)
        keeper.cart.guest_session.refresh_from_db()
        self.assertEqual(keeper.cart.guest_session.expires_at, keeper_expiry)
        self.assertEqual(keeper.cart.guest_session.token_hash, keeper_hash)
        self.assertEqual(Cart.objects.get().id, keeper.cart.id)

    def test_payload_interface_error_rolls_back_real_issuance(self):
        def fail(cart):
            self.assertEqual(GuestSession.objects.count(), 1)
            self.assertEqual(cart.guest_session_id, GuestSession.objects.get().id)
            raise InterfaceError("down")

        with patch("cart.views.cart_payload", side_effect=fail):
            response = self.client.post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "CART_TEMPORARILY_UNAVAILABLE")
        self.assertNotIn("guest_access", response.data)
        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(Cart.objects.exists())

    def test_serialization_failure_rolls_back_without_success(self):
        self.client.raise_request_exception = False
        original = JSONRenderer.render

        def fail(renderer, data, accepted_media_type=None, renderer_context=None):
            if not renderer_context:
                return original(renderer, data, accepted_media_type, renderer_context)
            self.assertTrue(transaction.get_connection().in_atomic_block)
            self.assertEqual(GuestSession.objects.count(), 1)
            raise RuntimeError("issuance report")

        with patch.object(JSONRenderer, "render", fail):
            response = self.client.post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )

        self.assertEqual(response.status_code, 500)
        self.assertNotEqual(response.status_code, 201)
        self.assertNotIn(b'"success": true', response.content)
        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(Cart.objects.exists())

    def test_later_start_succeeds_after_a_rolled_back_failure(self):
        with patch("cart.views.cart_payload", side_effect=OperationalError("down")):
            failed = self.client.post(
                reverse("create_cart"),
                {"action": "start"},
                format="json",
            )

        self.assertEqual(failed.status_code, 503)
        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(Cart.objects.exists())

        started = self.client.post(
            reverse("create_cart"),
            {"action": "start"},
            format="json",
        )

        self.assertEqual(started.status_code, 201)
        self.assertEqual(GuestSession.objects.count(), 1)
        self.assertEqual(Cart.objects.count(), 1)
        self.assertIn("guest_access", started.data)


_ISSUED_TOKEN = "LocalIssuanceCredentialValue" + ("0" * 15)


@override_settings(
    DEBUG=False,
    ADMINS=[("Ops", "ops@example.com")],
    CART_APP_CREDENTIAL="0123456789abcdef" * 4,
)
class CartIssuanceReportTests(APITransactionTestCase):
    client_class = CartAPIClient
    def test_issuance_failure_redacts_the_new_credential(self):
        self.assertEqual(len(_ISSUED_TOKEN), 43)
        token_hash = hashlib.sha256(_ISSUED_TOKEN.encode("utf-8")).hexdigest()
        seen = {}

        def fail(cart):
            seen["stored"] = GuestSession.objects.filter(token_hash=token_hash).exists()
            raise RuntimeError("issuance report")

        mail.outbox.clear()
        self.client.raise_request_exception = False
        self.client.cookies["cart_id"] = _ISSUED_TOKEN
        request_logger = logging.getLogger("django.request")
        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        handler = _Capture()
        request_logger.addHandler(handler)
        try:
            with patch("cart.guest_access.secrets.token_urlsafe", return_value=_ISSUED_TOKEN):
                with patch("cart.views.cart_payload", side_effect=fail):
                    response = self.client.post(
                        reverse("create_cart"),
                        {"action": "start"},
                        format="json",
                    )
        finally:
            request_logger.removeHandler(handler)

        self.assertTrue(seen["stored"])
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        logged = "\n".join(captured)
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0].body
        for output in (body, logged, email):
            self.assertNotIn(_ISSUED_TOKEN, output)
            self.assertNotIn(token_hash, output)
        self.assertIn("HTTP_COOKIE", email)
        self.assertIn("********************", email)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(Cart.objects.exists())


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL row-lock evidence only. Skipped on the normal SQLite run.",
)
@cart_app_settings
class CartHttpPostgresLockTests(TransactionTestCase):
    def test_http_mutation_holds_the_session_until_revocation_can_proceed(self):
        issued = issue_guest_cart()
        product = _product(slug="locked-clip")
        token = issued.raw_token
        held = threading.Event()
        release = threading.Event()
        state = {}

        def pausing(**kwargs):
            held.set()
            if not release.wait(15):
                raise RuntimeError("session lock was not released")
            from cart.operations import add_product_to_cart

            return add_product_to_cart(**kwargs)

        def revoke():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                thread_connection.ensure_connection()
                state["pid"] = thread_connection.connection.get_backend_pid()
                if not held.wait(15):
                    state["error"] = "mutation did not announce its lock"
                    return
                from cart.guest_access import revoke_guest_session

                revoke_guest_session(raw_token=token)
                state["revoked"] = True
            except Exception as exc:
                state["error"] = exc
            finally:
                thread_connection.close()

        def add():
            from django.db import connection as thread_connection

            thread_connection.close()
            client = CartAPIClient()
            try:
                with patch("cart.views.add_product_to_cart", side_effect=pausing):
                    state["response"] = client.post(
                        reverse("add_to_cart"),
                        {"product_id": product.id, "quantity": 1, "options": []},
                        format="json",
                        HTTP_AUTHORIZATION=f"Bearer {token}",
                    )
            except Exception as exc:
                state["add_error"] = exc
            finally:
                thread_connection.close()

        revoker = threading.Thread(target=revoke)
        worker = threading.Thread(target=add)
        revoker.start()
        try:
            deadline = time.monotonic() + 15
            while "pid" not in state and "error" not in state and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn("pid", state)
            worker.start()
            self.assertTrue(held.wait(15))
            seen_lock_wait = False
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT wait_event_type
                        FROM pg_stat_activity
                        WHERE pid = %s
                        """,
                        [state["pid"]],
                    )
                    row = cursor.fetchone()
                if row and row[0] == "Lock":
                    seen_lock_wait = True
                    break
                time.sleep(0.05)
            self.assertTrue(seen_lock_wait, msg=f"revocation was not waiting: {state}")
        finally:
            release.set()
            held.set()
            worker.join(20)
            revoker.join(20)

        self.assertFalse(worker.is_alive())
        self.assertFalse(revoker.is_alive())
        self.assertNotIn("error", state)
        self.assertNotIn("add_error", state)
        self.assertEqual(state["response"].status_code, 201)
        self.assertTrue(state.get("revoked"))
        self.assertEqual(CartItem.objects.count(), 1)
        follow_up = CartAPIClient().get(
            reverse("get_cart"),
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(follow_up.status_code, 401)
        self.assertEqual(follow_up.data, _ACCESS)

    def test_concurrent_replace_missing_creates_one_cart(self):
        issued = issue_guest_cart()
        token = issued.raw_token
        session = issued.cart.guest_session
        expiry = session.expires_at
        token_hash = session.token_hash
        session_id = session.id
        issued.cart.delete()
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def call():
            from django.db import connection as thread_connection

            thread_connection.close()
            client = CartAPIClient()
            try:
                barrier.wait(15)
                response = client.post(
                    reverse("create_cart"),
                    {"action": "replace_missing"},
                    format="json",
                    HTTP_AUTHORIZATION=f"Bearer {token}",
                )
                results.append((response.status_code, response.data.get("code")))
            except Exception as exc:
                errors.append(exc)
            finally:
                thread_connection.close()

        workers = [threading.Thread(target=call), threading.Thread(target=call)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(20)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(status for status, _code in results), [200, 201])
        self.assertEqual(Cart.objects.filter(guest_session_id=session_id).count(), 1)
        cart = Cart.objects.get()
        self.assertEqual(cart.items.count(), 0)
        session.refresh_from_db()
        self.assertEqual(session.expires_at, expiry)
        self.assertEqual(session.token_hash, token_hash)
        self.assertIsNone(session.revoked_at)
