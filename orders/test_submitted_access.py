"""Clearing submitted basket lines, and reading that guest's orders."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db.models.query import QuerySet
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITransactionTestCase

from cart.guest_access import issue_guest_cart, revoke_guest_session
from cart.models import Cart
from cart.test_application_client import TEST_APP_CREDENTIAL, CartAPIClient
from store.models import ProductOption

from .models import Order, OrderCreationAttempt
from .test_public_orders import _CUSTOMER, _guest_cart
from .factories import build_catalogue


_SUMMARY_FIELDS = {
    "reference",
    "created_at",
    "currency",
    "full_total",
    "payment_status",
}


@override_settings(CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL)
class SubmittedOrderAccessTests(APITransactionTestCase):
    def setUp(self):
        self.catalogue = build_catalogue()
        self.issued = _guest_cart(self.catalogue)
        self.client = CartAPIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.issued.raw_token}")

    def _post(self, *, key, body=None, token=None):
        payload = dict(_CUSTOMER)
        if token is not None:
            payload["cart_review_token"] = token
        if body is not None:
            payload = body
        return self.client.post(
            "/api/orders/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def _reviewed_token(self):
        cart = self.client.get("/api/cart/")
        self.assertEqual(cart.status_code, 200, cart.content)
        return cart.data["cart"]["cart_review_token"]

    def _add_again(self, quantity=1):
        return self.client.post(
            "/api/cart/add/",
            {
                "product_id": self.catalogue["product"].id,
                "quantity": quantity,
                "options": [self.catalogue["no_lights"].id, self.catalogue["lwb"].id],
            },
            format="json",
        )

    def test_first_submission_keeps_the_snapshot_and_clears_those_lines(self):
        line_id = self.issued.cart.items.get().id
        option_ids = set(ProductOption.objects.values_list("id", flat=True))
        guest_id = self.issued.cart.guest_session_id
        created = self._post(key="clear-once-1", token=self._reviewed_token())
        self.assertEqual(created.status_code, 201, created.content)
        order = created.data["order"]
        self.assertEqual(order["full_total"], "825.00")
        self.assertEqual(order["payment_status"], "pending_deposit")
        saved = order["items"][0]
        self.assertEqual(saved["quantity"], 1)
        self.assertEqual(saved["configured_unit_price"], "825.00")
        self.assertEqual(
            {(row["group_name"], row["option_name"], row["price_adjustment"]) for row in saved["options"]},
            {("Wheelbase", "LWB", "130.00"), ("Lights", "None", "0.00")},
        )
        stored = Order.objects.get()
        self.assertEqual(stored.full_total, Decimal("825.00"))
        self.assertEqual(stored.items.get().configured_unit_price, Decimal("825.00"))
        self.assertFalse(self.issued.cart.items.filter(pk=line_id).exists())
        self.assertEqual(self.issued.cart.items.count(), 0)
        self.issued.cart.refresh_from_db()
        self.assertEqual(self.issued.cart.guest_session_id, guest_id)
        self.assertEqual(set(ProductOption.objects.values_list("id", flat=True)), option_ids)
        self.assertEqual(stored.items.count(), 1)
        empty = self.client.get("/api/cart/")
        self.assertEqual(empty.status_code, 200, empty.content)
        self.assertEqual(empty.data["cart"]["items"], [])

    def test_deletion_failures_keep_the_basket_and_store_nothing(self):
        line_ids = set(self.issued.cart.items.values_list("id", flat=True))
        token = self._reviewed_token()
        original_delete = QuerySet.delete

        def fail_line_delete(queryset, *args, **kwargs):
            if queryset.model._meta.label == "cart.CartItem":
                raise RuntimeError("line delete failed")
            return original_delete(queryset, *args, **kwargs)

        self.client.raise_request_exception = False
        with patch.object(QuerySet, "delete", fail_line_delete):
            during = self._post(key="delete-fails", token=token)
        self.assertEqual(during.status_code, 500)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderCreationAttempt.objects.exists())
        self.assertEqual(set(self.issued.cart.items.values_list("id", flat=True)), line_ids)

        def fail_before_commit():
            raise RuntimeError("before commit")

        with patch("orders.access._submission_ready_to_commit", side_effect=fail_before_commit):
            after = self._post(key="delete-fails", token=token)
        self.assertEqual(after.status_code, 500)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderCreationAttempt.objects.exists())
        self.assertEqual(set(self.issued.cart.items.values_list("id", flat=True)), line_ids)

    def test_retry_returns_the_order_when_the_basket_is_empty(self):
        token = self._reviewed_token()
        created = self._post(key="lost-response", token=token)
        reference = created.data["order"]["reference"]
        self.assertEqual(self.issued.cart.items.count(), 0)
        omitted = dict(_CUSTOMER)
        retry = self.client.post(
            "/api/orders/",
            omitted,
            format="json",
            HTTP_IDEMPOTENCY_KEY="lost-response",
        )
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.data["order"]["reference"], reference)
        self.assertEqual(retry.data["order"]["full_total"], "825.00")
        self.assertEqual(self.issued.cart.items.count(), 0)
        self.assertEqual(Order.objects.count(), 1)

    def test_replay_leaves_lines_added_after_the_order(self):
        token = self._reviewed_token()
        created = self._post(key="kept-lines", token=token)
        reference = created.data["order"]["reference"]
        added = self._add_again(quantity=2)
        self.assertEqual(added.status_code, 201, added.content)
        later_id = added.data["cart"]["items"][0]["item_id"]
        self.client.patch(
            "/api/cart/update/",
            {"item_id": later_id, "quantity": 4},
            format="json",
        )
        replay = self._post(key="kept-lines", token="0" * 64)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.data["order"]["reference"], reference)
        self.assertEqual(replay.data["order"]["full_total"], "825.00")
        self.assertEqual(Order.objects.get().full_total, Decimal("825.00"))
        self.assertEqual(self.issued.cart.items.get().id, later_id)
        self.assertEqual(self.issued.cart.items.get().quantity, 4)

    def test_list_and_detail_survive_an_empty_or_absent_cart(self):
        created = self._post(key="history-one", token=self._reviewed_token())
        reference = created.data["order"]["reference"]
        self.assertEqual(self.issued.cart.items.count(), 0)
        listed = self.client.get("/api/orders/?page=1")
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertEqual(listed.data["orders"][0]["reference"], reference)
        detail = self.client.get(f"/api/orders/{reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.data["order"]["full_total"], "825.00")

        self.issued.cart.delete()
        self.assertFalse(Cart.objects.filter(guest_session_id=self.issued.cart.guest_session_id).exists())
        replay = self.client.post(
            "/api/orders/",
            _CUSTOMER,
            format="json",
            HTTP_IDEMPOTENCY_KEY="history-one",
        )
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.data["order"]["reference"], reference)
        listed = self.client.get("/api/orders/?page=1")
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertEqual(listed.data["orders"][0]["reference"], reference)
        detail = self.client.get(f"/api/orders/{reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail["Cache-Control"], "no-store")

    def test_history_is_private_paginated_and_has_no_store(self):
        references = []
        for number in range(1, 22):
            if number > 1:
                added = self._add_again()
                self.assertEqual(added.status_code, 201, added.content)
            created = self._post(key=f"page-key-{number:02d}", token=self._reviewed_token())
            self.assertEqual(created.status_code, 201, created.content)
            references.append(created.data["order"]["reference"])

        page = self.client.get("/api/orders/?page=1")
        self.assertEqual(page.status_code, 200, page.content)
        self.assertEqual(set(page.data), {"success", "orders", "page", "has_more"})
        self.assertTrue(page.data["success"])
        self.assertEqual(page.data["page"], 1)
        self.assertTrue(page.data["has_more"])
        self.assertEqual(len(page.data["orders"]), 20)
        self.assertEqual(
            [row["reference"] for row in page.data["orders"]],
            list(reversed(references))[:20],
        )
        self.assertEqual(set(page.data["orders"][0]), _SUMMARY_FIELDS)
        self.assertEqual(page.data["orders"][0]["currency"], "GBP")
        self.assertEqual(page.data["orders"][0]["full_total"], "825.00")
        self.assertEqual(page.data["orders"][0]["payment_status"], "pending_deposit")
        self.assertEqual(page["Cache-Control"], "no-store")
        self.assertNotIn("customer_email", page.content.decode())
        self.assertNotIn(self.issued.raw_token, page.content.decode())

        second = self.client.get("/api/orders/?page=2")
        self.assertEqual(second.data["has_more"], False)
        self.assertEqual(
            [row["reference"] for row in second.data["orders"]],
            [references[0]],
        )
        empty_page = self.client.get("/api/orders/?page=3")
        self.assertEqual(empty_page.data["orders"], [])
        self.assertEqual(empty_page.data["has_more"], False)

        newest = Order.objects.get(reference=references[-1])
        previous = Order.objects.get(reference=references[-2])
        Order.objects.filter(pk__in=[newest.pk, previous.pk]).update(
            created_at=timezone.now() + timedelta(days=1)
        )
        tied = self.client.get("/api/orders/?page=1")
        higher, lower = sorted((newest, previous), key=lambda row: row.pk, reverse=True)
        self.assertEqual(tied.data["orders"][0]["reference"], higher.reference)
        self.assertEqual(tied.data["orders"][1]["reference"], lower.reference)

        for query in ("", "?page=0", "?page=01", "?page=no", "?page=1&guest=1"):
            rejected = self.client.get(f"/api/orders/{query}")
            self.assertEqual(rejected.status_code, 400, rejected.content)
            self.assertEqual(rejected.data["code"], "ORDER_REQUEST_INVALID")
            self.assertNotIn("orders", rejected.data)
            self.assertEqual(rejected["Cache-Control"], "no-store")

        other = issue_guest_cart()
        foreign = CartAPIClient().get(
            "/api/orders/?page=1",
            HTTP_AUTHORIZATION=f"Bearer {other.raw_token}",
        )
        self.assertEqual(foreign.status_code, 200, foreign.content)
        self.assertEqual(foreign.data["orders"], [])
        hidden = CartAPIClient().get(
            f"/api/orders/{references[-1]}/",
            HTTP_AUTHORIZATION=f"Bearer {other.raw_token}",
        )
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.data["code"], "ORDER_NOT_AVAILABLE")

        absent = CartAPIClient().get("/api/orders/?page=1")
        self.assertEqual(absent.status_code, 401)
        self.assertEqual(absent.data["code"], "CART_ACCESS_UNAVAILABLE")
        self.assertNotIn("orders", absent.data)

        session = self.issued.cart.guest_session
        session.expires_at = timezone.now() - timedelta(minutes=1)
        session.save(update_fields=["expires_at"])
        expired = self.client.get("/api/orders/?page=1")
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.data["code"], "CART_ACCESS_UNAVAILABLE")
        self.assertNotIn("orders", expired.data)

        session.expires_at = timezone.now() + timedelta(days=1)
        session.save(update_fields=["expires_at"])
        revoke_guest_session(raw_token=self.issued.raw_token)
        revoked = self.client.get("/api/orders/?page=1")
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(revoked.data["code"], "CART_ACCESS_UNAVAILABLE")
        self.assertNotIn("orders", revoked.data)
