"""Internal guest-session credential tests.

SQLite checks the helper results. It does not prove PostgreSQL row locks.
The lock test at the bottom runs only on PostgreSQL.
"""

import threading
import time
import unittest
from datetime import timedelta
from decimal import Decimal
from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from store.models import Category, Product

from .guest_access import (
    TOKEN_PATTERN,
    GuestAccessError,
    get_guest_cart,
    get_guest_session,
    issue_guest_cart,
    revoke_guest_session,
)
from .models import Cart, CartItem, GuestSession
from .test_application_client import CartAPIClient, cart_app_settings


class GuestSessionCredentialTests(TestCase):
    def test_issuance_creates_one_session_and_its_cart(self):
        issued = issue_guest_cart()

        self.assertEqual(GuestSession.objects.count(), 1)
        self.assertEqual(Cart.objects.count(), 1)
        session = GuestSession.objects.get()
        self.assertEqual(issued.cart.guest_session_id, session.id)
        self.assertEqual(session.cart.id, issued.cart.id)
        self.assertIsNone(session.revoked_at)
        self.assertEqual(session.expires_at - session.created_at, timedelta(days=30))
        self.assertRegex(issued.raw_token, TOKEN_PATTERN)
        self.assertNotIn(issued.raw_token, repr(issued))

    def test_database_stores_the_hash_only(self):
        issued = issue_guest_cart()
        session = GuestSession.objects.get()

        self.assertEqual(len(session.token_hash), 64)
        self.assertNotEqual(session.token_hash, issued.raw_token)
        self.assertNotIn(issued.raw_token, session.token_hash)
        stored = " ".join(
            str(value)
            for value in (
                session.id,
                session.token_hash,
                session.created_at,
                session.expires_at,
                session.revoked_at,
            )
        )
        self.assertNotIn(issued.raw_token, stored)

    def test_separate_issuances_are_distinct(self):
        first = issue_guest_cart()
        second = issue_guest_cart()

        self.assertNotEqual(first.raw_token, second.raw_token)
        self.assertNotEqual(first.cart.id, second.cart.id)
        self.assertNotEqual(
            first.cart.guest_session.token_hash,
            second.cart.guest_session.token_hash,
        )
        self.assertEqual(GuestSession.objects.count(), 2)

    def test_matching_token_and_cart_resolve(self):
        issued = issue_guest_cart()

        session = get_guest_session(raw_token=issued.raw_token)
        cart = get_guest_cart(cart_id=str(issued.cart.id), raw_token=issued.raw_token)

        self.assertEqual(session.id, issued.cart.guest_session_id)
        self.assertEqual(cart.id, issued.cart.id)

    def test_another_sessions_token_cannot_access_the_cart(self):
        issued = issue_guest_cart()
        other = issue_guest_cart()

        with self.assertRaises(GuestAccessError):
            get_guest_cart(cart_id=str(issued.cart.id), raw_token=other.raw_token)
        self.assertEqual(Cart.objects.get(pk=issued.cart.id).guest_session_id, issued.cart.guest_session_id)

    def test_cart_uuid_session_key_and_session_uuid_are_not_tokens(self):
        issued = issue_guest_cart()
        legacy = Cart.objects.create(session_key="browser-session")
        session = issued.cart.guest_session

        for raw_token in (
            str(issued.cart.id),
            legacy.session_key,
            str(session.id),
            "",
        ):
            with self.assertRaises(GuestAccessError):
                get_guest_cart(cart_id=str(issued.cart.id), raw_token=raw_token)
        session.refresh_from_db()
        self.assertIsNone(session.revoked_at)

    def test_malformed_token_and_cart_id_are_controlled_errors(self):
        issued = issue_guest_cart()
        presented = [
            None,
            43,
            issued.raw_token.encode("ascii"),
            issued.raw_token + " ",
            " " + issued.raw_token,
            "not-a-token",
        ]
        for candidate in (issued.raw_token.lower(), issued.raw_token.swapcase()):
            if candidate != issued.raw_token:
                presented.append(candidate)
        self.assertGreater(len(presented), 6)
        for raw_token in presented:
            with self.assertRaises(GuestAccessError) as caught:
                get_guest_session(raw_token=raw_token)
            self.assertEqual(str(caught.exception), "Cart access is not available.")

        for cart_id in (None, issued.cart.id, "not-a-uuid", 15):
            with self.assertRaises(GuestAccessError) as caught:
                get_guest_cart(cart_id=cart_id, raw_token=issued.raw_token)
            self.assertEqual(str(caught.exception), "Cart access is not available.")

    def test_expired_exact_expiry_and_revoked_sessions_are_rejected(self):
        issued = issue_guest_cart()
        session = issued.cart.guest_session
        moment = session.expires_at

        with patch("cart.guest_access.timezone.now", return_value=moment):
            with self.assertRaises(GuestAccessError):
                get_guest_cart(cart_id=str(issued.cart.id), raw_token=issued.raw_token)

        session.expires_at = timezone.now() - timedelta(seconds=1)
        session.save(update_fields=["expires_at"])
        with self.assertRaises(GuestAccessError):
            get_guest_session(raw_token=issued.raw_token)

        session.expires_at = timezone.now() + timedelta(days=1)
        session.revoked_at = timezone.now()
        session.save(update_fields=["expires_at", "revoked_at"])
        with self.assertRaises(GuestAccessError):
            get_guest_cart(cart_id=str(issued.cart.id), raw_token=issued.raw_token)

    def test_read_does_not_extend_expiry_or_change_the_cart(self):
        issued = issue_guest_cart()
        session = GuestSession.objects.get()
        cart = Cart.objects.get()
        original_expiry = session.expires_at
        original_updated = cart.updated_at
        CartItem.objects.create(
            cart=cart,
            product=_product(),
            quantity=1,
        )

        get_guest_cart(cart_id=str(cart.id), raw_token=issued.raw_token)
        get_guest_session(raw_token=issued.raw_token)

        session.refresh_from_db()
        cart.refresh_from_db()
        self.assertEqual(session.expires_at, original_expiry)
        self.assertEqual(session.token_hash, cart.guest_session.token_hash)
        self.assertEqual(cart.updated_at, original_updated)
        self.assertEqual(cart.items.get().quantity, 1)
        self.assertEqual(Cart.objects.count(), 1)

    def test_legacy_cart_cannot_be_claimed(self):
        legacy = Cart.objects.create(session_key="already-there")
        issued = issue_guest_cart()

        self.assertIsNone(legacy.guest_session_id)
        with self.assertRaises(GuestAccessError):
            get_guest_cart(cart_id=str(legacy.id), raw_token=issued.raw_token)
        legacy.refresh_from_db()
        self.assertIsNone(legacy.guest_session_id)
        self.assertNotEqual(issued.cart.id, legacy.id)

    def test_deleting_the_cart_leaves_the_guest_session(self):
        issued = issue_guest_cart()
        session_id = issued.cart.guest_session_id
        issued.cart.delete()

        self.assertFalse(Cart.objects.exists())
        self.assertTrue(GuestSession.objects.filter(pk=session_id).exists())

    def test_repeated_revocation_blocks_later_access(self):
        issued = issue_guest_cart()
        cart_id = issued.cart.id

        first = revoke_guest_session(raw_token=issued.raw_token)
        revoked_at = first.revoked_at
        second = revoke_guest_session(raw_token=issued.raw_token)

        self.assertEqual(second.revoked_at, revoked_at)
        self.assertTrue(Cart.objects.filter(pk=cart_id).exists())
        with self.assertRaises(GuestAccessError):
            get_guest_cart(cart_id=str(cart_id), raw_token=issued.raw_token)

    def test_session_uuid_cannot_revoke_another_session(self):
        issued = issue_guest_cart()
        with self.assertRaises(GuestAccessError):
            revoke_guest_session(raw_token=str(issued.cart.guest_session_id))
        issued.cart.guest_session.refresh_from_db()
        self.assertIsNone(issued.cart.guest_session.revoked_at)

    def test_issuance_failure_rolls_back_both_records(self):
        with patch(
            "cart.guest_access.Cart.objects.create",
            side_effect=RuntimeError("cart insert failed"),
        ):
            with self.assertRaises(RuntimeError):
                issue_guest_cart()

        self.assertFalse(GuestSession.objects.exists())
        self.assertFalse(Cart.objects.exists())

    def test_one_session_cannot_have_two_carts(self):
        issued = issue_guest_cart()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Cart.objects.create(guest_session=issued.cart.guest_session)
        self.assertEqual(Cart.objects.filter(guest_session=issued.cart.guest_session).count(), 1)


class GuestSessionTransactionTests(TransactionTestCase):
    def test_row_lock_requires_the_caller_transaction(self):
        issued = issue_guest_cart()

        with self.assertRaises(RuntimeError):
            get_guest_session(raw_token=issued.raw_token, for_update=True)

        with transaction.atomic():
            cart = get_guest_cart(
                cart_id=str(issued.cart.id),
                raw_token=issued.raw_token,
                for_update=True,
            )
        self.assertEqual(cart.id, issued.cart.id)


class GuestSessionAdminTests(TestCase):
    @override_settings(
        STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage"
    )
    def test_cart_admin_cannot_rewrite_the_guest_session(self):
        issued = issue_guest_cart()
        other = issue_guest_cart()
        cart = issued.cart
        staff = User.objects.create_superuser("cart-admin", "cart-admin@example.com", "password")
        self.client.force_login(staff)
        url = reverse("admin:cart_cart_change", args=[cart.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertNotIn("token_hash", body)
        self.assertNotIn(issued.raw_token, body)
        self.assertNotIn("guest_session", body)

        payload = _admin_form_values(body)
        payload["guest_session"] = str(other.cart.guest_session_id)
        payload["session_key"] = "forged-session"
        payload["_save"] = "Save"
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        cart.refresh_from_db()
        self.assertEqual(cart.guest_session_id, issued.cart.guest_session_id)
        self.assertEqual(cart.session_key, "forged-session")


@cart_app_settings
class GuestSessionPublicCartTests(APITestCase):
    client_class = CartAPIClient
    def test_existing_cart_response_has_no_credential_fields(self):
        issued = issue_guest_cart()
        denied = self.client.get(reverse("get_cart"), {"cart_id": str(issued.cart.id)})
        self.assertEqual(denied.status_code, 401)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}")
        response = self.client.get(reverse("get_cart"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data["cart"]),
            {"id", "items", "item_count", "total"},
        )
        encoded = str(response.data)
        self.assertNotIn(issued.raw_token, encoded)
        self.assertNotIn("token_hash", encoded)
        self.assertNotIn("guest_session", encoded)


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL row-lock evidence only. Skipped on the normal SQLite run.",
)
class GuestSessionPostgresLockTests(TransactionTestCase):
    def test_revocation_waits_for_the_session_lock(self):
        issued = issue_guest_cart()
        token = issued.raw_token
        lock_held = threading.Event()
        state = {}

        def revoke():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                thread_connection.ensure_connection()
                state["pid"] = thread_connection.connection.get_backend_pid()
                if not lock_held.wait(15):
                    state["error"] = "session lock was not announced"
                    return
                revoke_guest_session(raw_token=token)
                state["revoked"] = True
            except Exception as exc:
                state["error"] = exc
            finally:
                thread_connection.close()

        worker = threading.Thread(target=revoke)
        worker.start()
        try:
            deadline = time.monotonic() + 15
            while "pid" not in state and "error" not in state and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn("pid", state)

            with transaction.atomic():
                get_guest_session(raw_token=token, for_update=True)
                lock_held.set()
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
            lock_held.set()
            worker.join(20)

        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", state)
        self.assertTrue(state.get("revoked"))
        with self.assertRaises(GuestAccessError):
            get_guest_cart(cart_id=str(issued.cart.id), raw_token=token)


def _product():
    category = Category.objects.create(name="Clips", slug="guest-clips", type="product")
    return Product.objects.create(
        category=category,
        name="Clip",
        slug="guest-clip",
        price=Decimal("1.00"),
    )


class _AdminFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}
        self._textarea = None
        self._select = None
        self._selected = ""
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get("name")
        if tag == "input" and name:
            if attrs.get("type") == "submit":
                return
            if attrs.get("type") == "checkbox":
                if "checked" in attrs:
                    self.values[name] = attrs.get("value", "on")
                return
            self.values[name] = attrs.get("value", "")
        elif tag == "textarea" and name:
            self._textarea = name
            self._buffer = []
        elif tag == "select" and name:
            self._select = name
            self._selected = ""
        elif tag == "option" and self._select and "selected" in attrs:
            self._selected = attrs.get("value", "")

    def handle_data(self, data):
        if self._textarea is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "textarea" and self._textarea is not None:
            self.values[self._textarea] = "".join(self._buffer)
            self._textarea = None
        elif tag == "select" and self._select is not None:
            self.values[self._select] = self._selected
            self._select = None


def _admin_form_values(html):
    parser = _AdminFormParser()
    parser.feed(html)
    return parser.values
