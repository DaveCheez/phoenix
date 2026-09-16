from decimal import Decimal

from django.urls import reverse
from rest_framework.test import APITestCase

from store.models import Category, Product

from .models import Cart, CartItem


class CartAPITests(APITestCase):
    def setUp(self):
        category = Category.objects.create(
            name="Roof Racks",
            slug="roof-racks",
            type="product",
        )
        self.product = Product.objects.create(
            category=category,
            name="Retro Roof Rack",
            slug="retro-roof-rack",
            price=Decimal("695.00"),
        )
        self.cart = Cart.objects.create(session_key="test-session")

    def test_adding_same_product_increases_quantity(self):
        url = reverse("add_to_cart")
        payload = {
            "cart_id": str(self.cart.id),
            "product_id": self.product.id,
            "quantity": 1,
        }

        first = self.client.post(url, payload, format="json")
        second = self.client.post(url, payload, format="json")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 2)
        self.assertEqual(second.data["cart"]["item_count"], 2)
        self.assertEqual(second.data["cart"]["total"], "1390.00")

    def test_update_quantity_returns_fresh_cart(self):
        item = CartItem.objects.create(
            cart=self.cart,
            product=self.product,
            quantity=1,
        )

        response = self.client.patch(
            reverse("update_cart_item"),
            {
                "cart_id": str(self.cart.id),
                "item_id": item.id,
                "quantity": 3,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 3)
        self.assertEqual(response.data["cart"]["total"], "2085.00")

    def test_zero_quantity_removes_item(self):
        item = CartItem.objects.create(
            cart=self.cart,
            product=self.product,
            quantity=1,
        )

        response = self.client.patch(
            reverse("update_cart_item"),
            {
                "cart_id": str(self.cart.id),
                "item_id": item.id,
                "quantity": 0,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(CartItem.objects.filter(id=item.id).exists())
        self.assertEqual(response.data["cart"]["items"], [])

    def test_item_cannot_be_changed_through_another_cart(self):
        item = CartItem.objects.create(
            cart=self.cart,
            product=self.product,
            quantity=1,
        )
        other_cart = Cart.objects.create(session_key="other-session")

        response = self.client.patch(
            reverse("update_cart_item"),
            {
                "cart_id": str(other_cart.id),
                "item_id": item.id,
                "quantity": 5,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 1)

    def test_invalid_quantity_is_rejected(self):
        response = self.client.post(
            reverse("add_to_cart"),
            {
                "cart_id": str(self.cart.id),
                "product_id": self.product.id,
                "quantity": -1,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "INVALID_QUANTITY")
        self.assertFalse(CartItem.objects.exists())
