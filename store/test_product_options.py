from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework.test import APITestCase

from .models import Category, Product, ProductOption, ProductOptionGroup


class ProductOptionAPITests(APITestCase):
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
            description="Workshop-fitted roof rack.",
            price=Decimal("695.00"),
        )
        self.detail_url = reverse(
            "product-detail-api",
            kwargs={"slug": self.product.slug},
        )

    def _group(self, *, name, required=False, display_order=0, active=True, help_text=""):
        return ProductOptionGroup.objects.create(
            product=self.product,
            name=name,
            required=required,
            display_order=display_order,
            active=active,
            help_text=help_text,
        )

    def _option(self, group, *, name, price="0.00", display_order=0, active=True, is_default=False, sku=None):
        return ProductOption.objects.create(
            group=group,
            name=name,
            price_adjustment=Decimal(price),
            display_order=display_order,
            active=active,
            is_default=is_default,
            sku=sku,
        )

    def test_product_detail_returns_active_groups_and_options(self):
        group = self._group(name="Wheelbase", required=True, display_order=1, help_text="Choose a length")
        option = self._option(group, name="MWB", price="0.00", display_order=1, is_default=True)

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        groups = response.data["option_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["id"], group.id)
        self.assertEqual(groups[0]["name"], "Wheelbase")
        self.assertTrue(groups[0]["required"])
        self.assertEqual(groups[0]["help_text"], "Choose a length")
        self.assertEqual(groups[0]["display_order"], 1)
        self.assertEqual(len(groups[0]["options"]), 1)
        self.assertEqual(groups[0]["options"][0]["id"], option.id)
        self.assertEqual(groups[0]["options"][0]["name"], "MWB")
        self.assertTrue(groups[0]["options"][0]["is_default"])

    def test_inactive_groups_are_excluded(self):
        self._group(name="Visible", display_order=1, active=True)
        self._group(name="Hidden", display_order=2, active=False)

        response = self.client.get(self.detail_url)

        names = [group["name"] for group in response.data["option_groups"]]
        self.assertEqual(names, ["Visible"])

    def test_inactive_options_are_excluded(self):
        group = self._group(name="Lights", display_order=1)
        self._option(group, name="None", price="0.00", display_order=1, active=True)
        self._option(group, name="Spotlights", price="85.00", display_order=2, active=False)

        response = self.client.get(self.detail_url)

        names = [option["name"] for option in response.data["option_groups"][0]["options"]]
        self.assertEqual(names, ["None"])

    def test_groups_are_ordered_by_display_order_then_id(self):
        later = self._group(name="Lights", display_order=20)
        earlier = self._group(name="Wheelbase", display_order=10)
        same_order_second = self._group(name="Colour", display_order=10)

        response = self.client.get(self.detail_url)

        names = [group["name"] for group in response.data["option_groups"]]
        self.assertEqual(names, ["Wheelbase", "Colour", "Lights"])
        self.assertLess(earlier.id, same_order_second.id)
        self.assertEqual(later.name, "Lights")

    def test_options_are_ordered_by_display_order_then_id(self):
        group = self._group(name="Wheelbase", display_order=1)
        self._option(group, name="LWB", price="150.00", display_order=20)
        self._option(group, name="MWB", price="0.00", display_order=10)
        self._option(group, name="XLWB", price="250.00", display_order=10)

        response = self.client.get(self.detail_url)

        names = [option["name"] for option in response.data["option_groups"][0]["options"]]
        self.assertEqual(names, ["MWB", "XLWB", "LWB"])

    def test_price_and_price_adjustment_are_identical_two_decimal_strings(self):
        group = self._group(name="Lights", display_order=1)
        self._option(group, name="Spotlights", price="85.50", display_order=1)

        response = self.client.get(self.detail_url)
        option = response.data["option_groups"][0]["options"][0]

        self.assertEqual(option["price"], "85.50")
        self.assertEqual(option["price_adjustment"], "85.50")
        self.assertEqual(option["price"], option["price_adjustment"])
        self.assertIsInstance(option["price"], str)
        self.assertIsInstance(option["price_adjustment"], str)

    def test_zero_price_options_serialize_as_0_00(self):
        group = self._group(name="Lights", display_order=1)
        self._option(group, name="None", price="0.00", display_order=1)

        response = self.client.get(self.detail_url)
        option = response.data["option_groups"][0]["options"][0]

        self.assertEqual(option["price"], "0.00")
        self.assertEqual(option["price_adjustment"], "0.00")

    def test_paid_adjustments_serialize_correctly(self):
        group = self._group(name="Wheelbase", required=True, display_order=1)
        self._option(group, name="LWB", price="150.00", display_order=1, sku="RR-LWB")

        response = self.client.get(self.detail_url)
        option = response.data["option_groups"][0]["options"][0]

        self.assertEqual(option["name"], "LWB")
        self.assertEqual(option["price_adjustment"], "150.00")
        self.assertEqual(option["price"], "150.00")
        self.assertEqual(option["sku"], "RR-LWB")

    def test_negative_price_adjustments_fail_validation(self):
        group = self._group(name="Wheelbase", display_order=1)
        option = ProductOption(
            group=group,
            name="Invalid",
            price_adjustment=Decimal("-1.00"),
        )

        with self.assertRaises(ValidationError) as raised:
            option.full_clean()
        self.assertIn("price_adjustment", raised.exception.message_dict)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductOption.objects.create(
                    group=group,
                    name="Invalid DB",
                    price_adjustment=Decimal("-1.00"),
                )

        self.assertFalse(ProductOption.objects.filter(name="Invalid DB").exists())

    def test_only_one_default_option_may_exist_per_group(self):
        group = self._group(name="Wheelbase", display_order=1)
        ProductOption.objects.create(
            group=group,
            name="MWB",
            price_adjustment=Decimal("0.00"),
            is_default=True,
        )
        duplicate = ProductOption(
            group=group,
            name="LWB",
            price_adjustment=Decimal("150.00"),
            is_default=True,
        )

        with self.assertRaises(ValidationError) as raised:
            duplicate.full_clean()
        self.assertIn("is_default", raised.exception.message_dict)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductOption.objects.create(
                    group=group,
                    name="XLWB",
                    price_adjustment=Decimal("250.00"),
                    is_default=True,
                )

        self.assertEqual(ProductOption.objects.filter(group=group, is_default=True).count(), 1)

    def test_orphan_options_are_never_included(self):
        group = self._group(name="Wheelbase", display_order=1)
        self._option(group, name="MWB", price="0.00", display_order=1)
        ProductOption.objects.create(
            group=None,
            name="Orphan Extra",
            price_adjustment=Decimal("50.00"),
        )

        response = self.client.get(self.detail_url)
        payload = str(response.data)

        self.assertNotIn("Orphan Extra", payload)
        names = [
            option["name"]
            for group in response.data["option_groups"]
            for option in group["options"]
        ]
        self.assertEqual(names, ["MWB"])

    def test_existing_product_detail_fields_remain_compatible(self):
        group = self._group(name="Wheelbase", required=True, display_order=1)
        self._option(group, name="MWB", price="0.00", display_order=1, sku=None)

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data.keys()),
            {
                "id",
                "name",
                "slug",
                "description",
                "price",
                "option_groups",
                "productimage_set",
            },
        )
        self.assertEqual(response.data["id"], self.product.id)
        self.assertEqual(response.data["name"], "Retro Roof Rack")
        self.assertEqual(response.data["slug"], "retro-roof-rack")
        self.assertEqual(response.data["description"], "Workshop-fitted roof rack.")
        self.assertEqual(response.data["price"], "695.00")
        self.assertEqual(response.data["productimage_set"], [])

        group_payload = response.data["option_groups"][0]
        self.assertEqual(
            set(group_payload.keys()),
            {"id", "name", "required", "help_text", "display_order", "options"},
        )
        self.assertEqual(
            set(group_payload["options"][0].keys()),
            {
                "id",
                "name",
                "price_adjustment",
                "price",
                "sku",
                "is_default",
                "display_order",
            },
        )
        self.assertNotIn("active", group_payload)
        self.assertNotIn("active", group_payload["options"][0])

    def test_product_list_and_category_apis_do_not_expose_option_groups(self):
        self._group(name="Wheelbase", display_order=1)

        list_response = self.client.get(reverse("product-list-api"))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.data), 1)
        self.assertEqual(
            set(list_response.data[0].keys()),
            {"id", "name", "slug", "description", "price", "thumbnail"},
        )
        self.assertNotIn("option_groups", list_response.data[0])

        category_response = self.client.get(
            reverse("category-detail-api", kwargs={"slug": self.category.slug})
        )
        self.assertEqual(category_response.status_code, 200)
        self.assertNotIn("option_groups", category_response.data)
        self.assertEqual(len(category_response.data["products"]), 1)
        self.assertNotIn("option_groups", category_response.data["products"][0])
        self.assertEqual(
            set(category_response.data["products"][0].keys()),
            {"id", "name", "slug", "description", "price", "thumbnail"},
        )
