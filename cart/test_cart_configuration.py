from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from store.models import Category, Product, ProductOption, ProductOptionGroup

from .configuration import BLANK_CONFIGURATION_SIGNATURE, build_configuration_signature
from .admin import CartItemAdmin, CartItemOptionAdmin, CartItemOptionInline
from .guest_access import issue_guest_cart
from .test_application_client import CartAPIClient, cart_app_settings
from .models import Cart, CartItem, CartItemOption


CART_ITEM_PAYLOAD_KEYS = {
    "item_id",
    "product_id",
    "name",
    "product_slug",
    "quantity",
    "base_unit_price",
    "options_total",
    "configured_unit_price",
    "price",
    "line_total",
    "selected_options",
    "configuration_signature",
    "configuration_valid",
    "sku",
    "image",
}
CART_PAYLOAD_KEYS = {"id", "items", "item_count", "total"}


class ConfigurationSignatureTests(TestCase):
    def test_empty_iterable_returns_blank_signature(self):
        self.assertEqual(build_configuration_signature([]), "")
        self.assertEqual(build_configuration_signature(()), BLANK_CONFIGURATION_SIGNATURE)

    def test_signature_is_independent_of_option_order(self):
        self.assertEqual(build_configuration_signature([20, 11]), "11,20")
        self.assertEqual(build_configuration_signature([11, 20]), "11,20")
        self.assertEqual(build_configuration_signature([20, 11, 3]), "3,11,20")

    def test_duplicate_option_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            build_configuration_signature([11, 11])

    def test_invalid_option_ids_are_rejected(self):
        for value in (0, -1, True, False, None, "11", "abc", 1.5, {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    build_configuration_signature([value])


class CartConfigurationModelTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Roof Racks",
            slug="roof-racks",
            type="product",
        )
        self.product = Product.objects.create(
            category=self.category,
            name="Retro Roof Rack",
            slug="retro-roof-rack",
            price=Decimal("695.00"),
        )
        self.other_product = Product.objects.create(
            category=self.category,
            name="Side Steps",
            slug="side-steps",
            price=Decimal("420.00"),
        )
        self.cart = Cart.objects.create(session_key="config-session")
        self.group = ProductOptionGroup.objects.create(
            product=self.product,
            name="Wheelbase",
            required=True,
        )
        self.other_group = ProductOptionGroup.objects.create(
            product=self.product,
            name="Lights",
        )
        self.foreign_group = ProductOptionGroup.objects.create(
            product=self.other_product,
            name="Colour",
        )
        self.mwb = ProductOption.objects.create(
            group=self.group,
            name="MWB",
            price_adjustment=Decimal("0.00"),
        )
        self.lwb = ProductOption.objects.create(
            group=self.group,
            name="LWB",
            price_adjustment=Decimal("150.00"),
        )
        self.lights = ProductOption.objects.create(
            group=self.other_group,
            name="Spotlights",
            price_adjustment=Decimal("85.00"),
        )
        self.foreign_option = ProductOption.objects.create(
            group=self.foreign_group,
            name="Red",
            price_adjustment=Decimal("0.00"),
        )

    def _item(self, *, product=None, signature="", quantity=1):
        return CartItem.objects.create(
            cart=self.cart,
            product=product or self.product,
            quantity=quantity,
            configuration_signature=signature,
        )

    def test_same_cart_and_product_can_have_different_signatures(self):
        first = self._item(signature="")
        second = self._item(signature="11,20")
        self.assertEqual(CartItem.objects.filter(cart=self.cart, product=self.product).count(), 2)
        self.assertNotEqual(first.configuration_signature, second.configuration_signature)

    def test_same_cart_product_and_signature_cannot_be_duplicated(self):
        self._item(signature="11,20")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._item(signature="11,20")

    def test_different_products_may_use_the_same_signature(self):
        self._item(product=self.product, signature="11,20")
        other = self._item(product=self.other_product, signature="11,20")
        self.assertEqual(other.configuration_signature, "11,20")
        self.assertEqual(CartItem.objects.filter(configuration_signature="11,20").count(), 2)

    def test_one_selected_option_per_group(self):
        item = self._item(signature="unused")
        CartItemOption.objects.create(cart_item=item, group=self.group, option=self.mwb)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CartItemOption.objects.create(
                    cart_item=item,
                    group=self.group,
                    option=self.lwb,
                )

    def test_same_option_cannot_be_repeated_on_one_cart_item(self):
        item = self._item(signature="unused")
        CartItemOption.objects.create(cart_item=item, group=self.group, option=self.mwb)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CartItemOption.objects.create(
                    cart_item=item,
                    group=self.group,
                    option=self.mwb,
                )

    def test_full_clean_rejects_option_group_mismatch(self):
        item = self._item(signature="unused")
        selection = CartItemOption(
            cart_item=item,
            group=self.group,
            option=self.lights,
        )
        with self.assertRaises(ValidationError) as raised:
            selection.full_clean()
        self.assertIn("option", raised.exception.message_dict)

    def test_full_clean_rejects_group_for_a_different_product(self):
        item = self._item(signature="unused")
        selection = CartItemOption(
            cart_item=item,
            group=self.foreign_group,
            option=self.foreign_option,
        )
        with self.assertRaises(ValidationError) as raised:
            selection.full_clean()
        self.assertIn("group", raised.exception.message_dict)

    def test_deleting_a_selected_option_is_protected(self):
        item = self._item(signature="unused")
        CartItemOption.objects.create(cart_item=item, group=self.group, option=self.mwb)
        with self.assertRaises(ProtectedError):
            self.mwb.delete()
        self.assertTrue(ProductOption.objects.filter(id=self.mwb.id).exists())
        self.assertTrue(CartItemOption.objects.filter(option=self.mwb).exists())

    def test_deleting_a_selected_group_is_protected(self):
        item = self._item(signature="unused")
        CartItemOption.objects.create(cart_item=item, group=self.group, option=self.mwb)
        with self.assertRaises(ProtectedError):
            self.group.delete()
        self.assertTrue(ProductOptionGroup.objects.filter(id=self.group.id).exists())
        self.assertTrue(CartItemOption.objects.filter(group=self.group).exists())


@cart_app_settings
class CartConfigurationAPITests(APITestCase):
    client_class = CartAPIClient
    def setUp(self):
        self.category = Category.objects.create(
            name="Roof Racks",
            slug="roof-racks",
            type="product",
        )
        self.product = Product.objects.create(
            category=self.category,
            name="Retro Roof Rack",
            slug="retro-roof-rack",
            price=Decimal("695.00"),
        )
        issued = issue_guest_cart()
        self.cart = issued.cart
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issued.raw_token}")
        self.add_url = reverse("add_to_cart")

    def _add(self, extra=None, quantity=1):
        payload = {
            "cart_id": str(self.cart.id),
            "product_id": self.product.id,
            "quantity": quantity,
        }
        if extra:
            payload.update(extra)
        return self.client.post(self.add_url, payload, format="json")

    def test_add_without_options_stores_a_blank_configuration_signature(self):
        response = self._add()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["created"])
        item = CartItem.objects.get()
        self.assertEqual(item.configuration_signature, "")
        self.assertFalse(CartItemOption.objects.exists())

    def test_adding_the_same_product_without_options_increments_quantity(self):
        first = self._add()
        second = self._add()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.data["created"])
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 2)
        self.assertEqual(CartItem.objects.get().configuration_signature, "")

    def test_existing_get_update_remove_and_clear_cart_apis_still_work(self):
        add = self._add(quantity=1)
        item_id = add.data["cart"]["items"][0]["item_id"]

        get_response = self.client.get(
            reverse("get_cart"),
            {"cart_id": str(self.cart.id)},
        )
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.data["cart"]["item_count"], 1)

        update = self.client.patch(
            reverse("update_cart_item"),
            {
                "cart_id": str(self.cart.id),
                "item_id": item_id,
                "quantity": 3,
            },
            format="json",
        )
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.data["cart"]["items"][0]["quantity"], 3)

        remove = self.client.delete(
            reverse("remove_from_cart"),
            {
                "cart_id": str(self.cart.id),
                "item_id": item_id,
            },
            format="json",
        )
        self.assertEqual(remove.status_code, 200)
        self.assertEqual(remove.data["cart"]["items"], [])

        self._add()
        clear = self.client.delete(
            reverse("clear_cart"),
            {"cart_id": str(self.cart.id)},
            format="json",
        )
        self.assertEqual(clear.status_code, 200)
        self.assertEqual(clear.data["cart"]["items"], [])
        self.assertEqual(clear.data["cart"]["total"], "0.00")

    def test_public_cart_payload_still_uses_base_product_pricing(self):
        response = self._add(quantity=2)
        cart = response.data["cart"]
        item = cart["items"][0]

        self.assertEqual(set(cart.keys()), CART_PAYLOAD_KEYS)
        self.assertEqual(set(item.keys()), CART_ITEM_PAYLOAD_KEYS)
        self.assertNotIn("options", item)
        self.assertEqual(item["selected_options"], [])
        self.assertEqual(item["configuration_signature"], "")
        self.assertTrue(item["configuration_valid"])
        self.assertEqual(item["base_unit_price"], "695.00")
        self.assertEqual(item["options_total"], "0.00")
        self.assertEqual(item["configured_unit_price"], "695.00")
        self.assertEqual(item["price"], "695.00")
        self.assertEqual(item["line_total"], "1390.00")
        self.assertEqual(cart["total"], "1390.00")

    def test_frontend_options_object_is_rejected(self):
        response = self._add(
            extra={
                "options": {
                    "Wheelbase": 11,
                    "Lights": 20,
                }
            }
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["success"], False)
        self.assertEqual(response.data["code"], "INVALID_OPTIONS_FORMAT")
        self.assertEqual(
            response.data["error"],
            "Your product selections could not be read. "
            "Refresh the product page and select your options again.",
        )
        self.assertFalse(CartItem.objects.exists())
        self.assertFalse(CartItemOption.objects.exists())


class CartAdminInspectionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_superuser(
            "admin",
            "admin@example.com",
            "password",
        )
        self.request = self.factory.get("/admin/")
        self.request.user = self.user
        self.site = AdminSite()

    def test_cart_item_admin_is_restricted_to_quantity_edits(self):
        item_admin = CartItemAdmin(CartItem, self.site)
        self.assertFalse(item_admin.has_add_permission(self.request))
        self.assertTrue(item_admin.has_change_permission(self.request))
        self.assertTrue(item_admin.has_delete_permission(self.request))
        self.assertEqual(
            list(item_admin.readonly_fields),
            [
                "cart",
                "product",
                "configuration_signature",
                "created_at",
                "updated_at",
            ],
        )
        self.assertNotIn("quantity", item_admin.readonly_fields)
        self.assertEqual(
            list(item_admin.list_display),
            [
                "id",
                "cart",
                "product",
                "quantity",
                "configuration_signature",
                "created_at",
                "updated_at",
            ],
        )
        self.assertEqual(item_admin.list_filter, ["product"])
        self.assertEqual(
            item_admin.search_fields,
            ["product__name", "configuration_signature"],
        )

    def test_cart_item_option_admin_is_view_only(self):
        option_admin = CartItemOptionAdmin(CartItemOption, self.site)
        self.assertFalse(option_admin.has_add_permission(self.request))
        self.assertFalse(option_admin.has_change_permission(self.request))
        self.assertFalse(option_admin.has_delete_permission(self.request))
        self.assertEqual(
            list(option_admin.readonly_fields),
            ["cart_item", "group", "option", "created_at"],
        )

    def test_cart_item_option_inline_is_inspection_only(self):
        inline = CartItemOptionInline(CartItem, self.site)
        self.assertEqual(inline.extra, 0)
        self.assertEqual(inline.max_num, 0)
        self.assertFalse(inline.can_delete)
        self.assertEqual(inline.fields, ["group", "option", "created_at"])
        self.assertEqual(inline.readonly_fields, ["group", "option", "created_at"])
        self.assertEqual(inline.ordering, ("group_id", "option_id", "id"))
        self.assertFalse(inline.has_add_permission(self.request))
        self.assertFalse(inline.has_change_permission(self.request))
        self.assertFalse(inline.has_delete_permission(self.request))
