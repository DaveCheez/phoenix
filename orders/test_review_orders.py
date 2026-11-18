"""API coverage for the basket review marker on unpaid orders."""

from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from cart.guest_access import issue_guest_cart, revoke_guest_session
from cart.test_application_client import TEST_APP_CREDENTIAL, CartAPIClient
from store.models import Category, Product, ProductOption, ProductOptionGroup

from .access import customer_request_hash
from .models import Order, OrderCreationAttempt
from .money import calculate_deposit
from .review import review_document_for_lines


_CUSTOMER = {
    "customer_name": "Alex Farmer",
    "customer_email": "alex@example.com",
    "customer_phone": "01254 000000",
    "vehicle_make": "Volkswagen",
    "vehicle_model": "T6.1",
    "vehicle_year": 2021,
    "vehicle_wheelbase": "LWB",
}


def _catalogue():
    category = Category.objects.create(name="Demo", slug="review-demo", type="product")
    product = Product.objects.create(
        category=category,
        name="Review Panel",
        slug="review-panel",
        price=Decimal("100.00"),
    )
    specification = ProductOptionGroup.objects.create(
        product=product,
        name="Specification",
        required=True,
        display_order=10,
    )
    extra = ProductOptionGroup.objects.create(
        product=product,
        name="Extra",
        required=False,
        display_order=20,
    )
    standard = ProductOption.objects.create(
        group=specification,
        name="Standard",
        price_adjustment=Decimal("0.00"),
        display_order=10,
        is_default=True,
    )
    enhanced = ProductOption.objects.create(
        group=specification,
        name="Enhanced",
        price_adjustment=Decimal("25.00"),
        sku="ENH",
        display_order=20,
    )
    none = ProductOption.objects.create(
        group=extra,
        name="None",
        price_adjustment=Decimal("0.00"),
        display_order=10,
        is_default=True,
    )
    return {
        "product": product,
        "standard": standard,
        "enhanced": enhanced,
        "none": none,
    }


@override_settings(CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL)
class UnpaidOrderReviewTests(APITestCase):
    client_class = CartAPIClient

    def setUp(self):
        self.catalogue = _catalogue()
        self.issued = issue_guest_cart()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.issued.raw_token}")

    def _add(self, options, quantity=1):
        return self.client.post(
            "/api/cart/add/",
            {
                "product_id": self.catalogue["product"].id,
                "quantity": quantity,
                "options": options,
            },
            format="json",
        )

    def _order(self, token, *, key, body=None):
        payload = {**_CUSTOMER, "cart_review_token": token}
        if body is not None:
            payload = body
        return self.client.post(
            "/api/orders/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_cart_add_creates_and_confirms_an_unpaid_snapshot(self):
        enhanced = self.catalogue["enhanced"]
        none = self.catalogue["none"]
        standard = self.catalogue["standard"]
        missing = self._add([none.id])
        self.assertEqual(missing.status_code, 400, missing.content)
        self.assertEqual(missing.data["code"], "MISSING_REQUIRED_OPTION")

        first = self._add([enhanced.id, none.id])
        self.assertEqual(first.status_code, 201, first.content)
        again = self._add([none.id, enhanced.id])
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(len(again.data["cart"]["items"]), 1)
        self.assertEqual(again.data["cart"]["items"][0]["quantity"], 2)
        other = self._add([standard.id, none.id])
        self.assertEqual(other.status_code, 201, other.content)
        self.assertEqual(len(other.data["cart"]["items"]), 2)

        reviewed = self.client.get("/api/cart/")
        self.assertEqual(reviewed.status_code, 200, reviewed.content)
        cart = reviewed.data["cart"]
        token = cart["cart_review_token"]
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        self.assertNotIn(self.issued.raw_token, reviewed.content.decode())
        priced = {item["configured_unit_price"]: item for item in cart["items"]}
        self.assertEqual(priced["125.00"]["quantity"], 2)
        self.assertEqual(priced["125.00"]["base_unit_price"], "100.00")
        self.assertEqual(
            {
                option["option_name"]: option["price_adjustment"]
                for option in priced["125.00"]["selected_options"]
            },
            {"Enhanced": "25.00", "None": "0.00"},
        )
        self.assertEqual(
            {
                option["option_name"]: option["price_adjustment"]
                for option in priced["100.00"]["selected_options"]
            },
            {"Standard": "0.00", "None": "0.00"},
        )

        created = self._order(token, key="review-key-1")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertTrue(created.data["success"])
        order = created.data["order"]
        split = calculate_deposit(Decimal("350.00"))
        self.assertEqual(order["full_total"], "350.00")
        self.assertEqual(order["deposit_required"], format(split.deposit, "f"))
        self.assertEqual(order["balance_on_completion"], format(split.balance, "f"))
        self.assertEqual(order["payment_status"], "pending_deposit")
        self.assertEqual(order["fitting_charge"], "0.00")
        self.assertEqual(order["tax_amount"], "0.00")
        saved = {
            item["configured_unit_price"]: item for item in order["items"]
        }
        self.assertEqual(saved["125.00"]["quantity"], 2)
        self.assertEqual(saved["125.00"]["line_total"], "250.00")
        self.assertEqual(
            {
                (option["group_name"], option["option_name"], option["price_adjustment"])
                for option in saved["125.00"]["options"]
            },
            {("Specification", "Enhanced", "25.00"), ("Extra", "None", "0.00")},
        )
        self.assertEqual(
            {
                (option["group_name"], option["option_name"], option["price_adjustment"])
                for option in saved["100.00"]["options"]
            },
            {("Specification", "Standard", "0.00"), ("Extra", "None", "0.00")},
        )
        stored = Order.objects.get()
        self.assertEqual(stored.amount_paid, Decimal("0.00"))
        self.assertEqual(stored.payment_status, "pending_deposit")
        self.assertEqual(stored.source_cart_id, self.issued.cart.id)
        self.assertEqual(self.issued.cart.items.count(), 2)
        self.assertEqual(
            OrderCreationAttempt.objects.get().request_hash,
            customer_request_hash(
                **_CUSTOMER,
                vehicle_registration="",
                customer_notes="",
            )[0],
        )

        confirmed = self.client.get(f"/api/orders/{order['reference']}/")
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        self.assertEqual(
            {item["line_total"] for item in confirmed.data["order"]["items"]},
            {"250.00", "100.00"},
        )

        second = self._order(token, key="review-key-2")
        self.assertEqual(second.status_code, 201, second.content)
        self.assertNotEqual(second.data["order"]["reference"], order["reference"])
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(
            set(Order.objects.values_list("source_cart_id", flat=True)),
            {self.issued.cart.id},
        )
        self.assertEqual(self.issued.cart.items.count(), 2)
        for row in Order.objects.all():
            self.assertEqual(row.payment_status, "pending_deposit")
            self.assertEqual(row.amount_paid, Decimal("0.00"))

    def test_browser_prices_and_a_missing_marker_leave_no_attempt(self):
        added = self._add([self.catalogue["enhanced"].id, self.catalogue["none"].id])
        token = added.data["cart"]["cart_review_token"]
        before_orders = Order.objects.count()
        before_attempts = OrderCreationAttempt.objects.count()
        for field, value in (
            ("items", [{"product_id": 1}]),
            ("full_total", "1.00"),
            ("deposit", "1.00"),
            ("deposit_required", "1.00"),
            ("tax_amount", "1.00"),
            ("fitting_charge", "1.00"),
            ("payment_status", "paid"),
            ("shipping_address", "1 High Street"),
        ):
            response = self._order(
                token,
                key=f"reject-{field}",
                body={**_CUSTOMER, "cart_review_token": token, field: value},
            )
            self.assertEqual(response.status_code, 400, response.content)
            self.assertEqual(response.data["code"], "ORDER_REQUEST_INVALID")
            self.assertFalse(response.data["success"])
        for label, bad in (
            ("absent", None),
            ("blank", ""),
            ("short", "abc"),
            ("upper", "A" * 64),
            ("number", 1),
            ("list", ["x"]),
        ):
            body = dict(_CUSTOMER)
            if bad is not None:
                body["cart_review_token"] = bad
            response = self._order(token, key=f"missing-{label}", body=body)
            self.assertEqual(response.status_code, 400, response.content)
            self.assertEqual(response.data["code"], "CART_REVIEW_REQUIRED")
        self.assertEqual(Order.objects.count(), before_orders)
        self.assertEqual(OrderCreationAttempt.objects.count(), before_attempts)

    def test_changed_basket_or_price_conflicts_without_a_partial_attempt(self):
        added = self._add([self.catalogue["standard"].id, self.catalogue["none"].id])
        token = added.data["cart"]["cart_review_token"]
        item_id = added.data["cart"]["items"][0]["item_id"]
        changed_quantity = self.client.patch(
            "/api/cart/update/",
            {"item_id": item_id, "quantity": 3},
            format="json",
        )
        self.assertEqual(changed_quantity.status_code, 200, changed_quantity.content)
        quantity_conflict = self._order(token, key="qty-changed")
        self.assertEqual(quantity_conflict.status_code, 409, quantity_conflict.content)
        self.assertEqual(quantity_conflict.data["code"], "CART_CHANGED")
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderCreationAttempt.objects.exists())

        current = changed_quantity.data["cart"]["cart_review_token"]
        self.client.delete("/api/cart/remove/", {"item_id": item_id}, format="json")
        replaced = self._add([self.catalogue["enhanced"].id, self.catalogue["none"].id])
        self.assertEqual(replaced.status_code, 201, replaced.content)
        option_conflict = self._order(current, key="opt-changed")
        self.assertEqual(option_conflict.status_code, 409)
        self.assertEqual(option_conflict.data["code"], "CART_CHANGED")

        fresh = replaced.data["cart"]["cart_review_token"]
        product = self.catalogue["product"]
        product.price = Decimal("150.00")
        product.save(update_fields=["price"])
        price_conflict = self._order(fresh, key="price-key")
        self.assertEqual(price_conflict.status_code, 409, price_conflict.content)
        self.assertEqual(price_conflict.data["code"], "CART_CHANGED")
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderCreationAttempt.objects.exists())
        self.assertEqual(self.issued.cart.items.count(), 1)

        reviewed = self.client.get("/api/cart/")
        created = self._order(reviewed.data["cart"]["cart_review_token"], key="price-ok")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.data["order"]["items"][0]["configured_unit_price"], "175.00")
        self.assertEqual(self.issued.cart.items.count(), 1)

    def test_same_key_returns_the_original_snapshot_after_the_basket_changes(self):
        added = self._add([self.catalogue["enhanced"].id, self.catalogue["none"].id])
        token = added.data["cart"]["cart_review_token"]
        item_id = added.data["cart"]["items"][0]["item_id"]
        created = self._order(token, key="retry-key")
        self.assertEqual(created.status_code, 201, created.content)
        reference = created.data["order"]["reference"]
        original_total = created.data["order"]["full_total"]

        self.catalogue["product"].price = Decimal("180.00")
        self.catalogue["product"].save(update_fields=["price"])
        self.client.patch(
            "/api/cart/update/",
            {"item_id": item_id, "quantity": 4},
            format="json",
        )
        retry = self._order(token, key="retry-key")
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.data["order"]["reference"], reference)
        self.assertEqual(retry.data["order"]["full_total"], original_total)
        self.assertEqual(retry.data["order"]["items"][0]["configured_unit_price"], "125.00")
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(self.issued.cart.items.get().quantity, 4)

        stale_marker = self._order("0" * 64, key="retry-key")
        self.assertEqual(stale_marker.status_code, 200, stale_marker.content)
        self.assertEqual(stale_marker.data["order"]["reference"], reference)

        conflict = self._order(
            token,
            key="retry-key",
            body={
                **_CUSTOMER,
                "customer_name": "Sam Farmer",
                "cart_review_token": token,
            },
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(Order.objects.get().customer_name, "Alex Farmer")
        self.assertEqual(Order.objects.get().full_total, Decimal(original_total))

    def test_confirmation_stays_with_the_creating_guest(self):
        added = self._add([self.catalogue["standard"].id])
        created = self._order(added.data["cart"]["cart_review_token"], key="owner-key")
        reference = created.data["order"]["reference"]
        other = issue_guest_cart()
        denied = CartAPIClient().get(
            f"/api/orders/{reference}/",
            HTTP_AUTHORIZATION=f"Bearer {other.raw_token}",
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.data["code"], "ORDER_NOT_AVAILABLE")
        bare = CartAPIClient().get(f"/api/orders/{reference}/")
        self.assertEqual(bare.status_code, 401)
        self.assertEqual(bare.data["code"], "CART_ACCESS_UNAVAILABLE")

        session = self.issued.cart.guest_session
        session.expires_at = timezone.now() - timedelta(seconds=1)
        session.save(update_fields=["expires_at"])
        expired = self.client.get(f"/api/orders/{reference}/")
        self.assertEqual(expired.status_code, 401)

        session.expires_at = timezone.now() + timedelta(days=1)
        session.save(update_fields=["expires_at"])
        revoke_guest_session(raw_token=self.issued.raw_token)
        revoked = self.client.get(f"/api/orders/{reference}/")
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(Order.objects.get().reference, reference)

    def test_review_document_keeps_prices_and_omits_volatile_fields(self):
        added = self._add([self.catalogue["enhanced"].id, self.catalogue["none"].id])
        self.assertEqual(added.status_code, 201, added.content)
        from .creation import _snapshot_lines

        document = review_document_for_lines(_snapshot_lines(self.issued.cart))
        encoded_keys = _keys(document)
        self.assertNotIn("image", encoded_keys)
        self.assertNotIn("created_at", encoded_keys)
        self.assertNotIn("updated_at", encoded_keys)
        self.assertNotIn("item_id", encoded_keys)
        self.assertIn("product_id", encoded_keys)
        self.assertIn("option_id", encoded_keys)
        self.assertIn("price_adjustment", encoded_keys)
        self.assertIn("deposit_required", encoded_keys)
        self.assertEqual(
            {option["price_adjustment"] for option in document["items"][0]["options"]},
            {"25.00", "0.00"},
        )
        self.assertEqual(document["terms"]["full_total"], "125.00")


def _keys(value):
    found = set()
    if isinstance(value, dict):
        found.update(value)
        for child in value.values():
            found.update(_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_keys(child))
    return found
