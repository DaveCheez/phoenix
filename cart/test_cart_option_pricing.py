from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APITestCase

from store.models import Category, Product, ProductOption, ProductOptionGroup

from .models import Cart, CartItem, CartItemOption


class CartOptionPricingTests(APITestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Roof Racks",
            slug="roof-racks-pricing",
            type="product",
        )
        self.product = Product.objects.create(
            category=self.category,
            name="Retro Roof Rack",
            slug="retro-roof-rack-pricing",
            price=Decimal("695.00"),
        )
        self.plain_product = Product.objects.create(
            category=self.category,
            name="Plain Rack",
            slug="plain-rack",
            price=Decimal("500.00"),
        )
        self.other_product = Product.objects.create(
            category=self.category,
            name="Side Steps",
            slug="side-steps-pricing",
            price=Decimal("420.00"),
        )
        self.cart = Cart.objects.create(session_key="pricing-session")
        self.add_url = reverse("add_to_cart")

        self.wheelbase = ProductOptionGroup.objects.create(
            product=self.product,
            name="Wheelbase",
            required=True,
            display_order=10,
        )
        self.lights = ProductOptionGroup.objects.create(
            product=self.product,
            name="Lights",
            required=False,
            display_order=20,
        )
        self.mwb = ProductOption.objects.create(
            group=self.wheelbase,
            name="MWB",
            price_adjustment=Decimal("0.00"),
            display_order=10,
            is_default=True,
        )
        self.lwb = ProductOption.objects.create(
            group=self.wheelbase,
            name="LWB",
            price_adjustment=Decimal("150.00"),
            display_order=20,
        )
        self.no_lights = ProductOption.objects.create(
            group=self.lights,
            name="None",
            price_adjustment=Decimal("0.00"),
            display_order=10,
            is_default=True,
        )
        self.spotlights = ProductOption.objects.create(
            group=self.lights,
            name="Spotlights",
            price_adjustment=Decimal("85.00"),
            display_order=20,
        )
        self.foreign_group = ProductOptionGroup.objects.create(
            product=self.other_product,
            name="Colour",
            required=True,
        )
        self.foreign_option = ProductOption.objects.create(
            group=self.foreign_group,
            name="Red",
            price_adjustment=Decimal("25.00"),
        )
        self.orphan = ProductOption.objects.create(
            group=None,
            name="Orphan Extra",
            price_adjustment=Decimal("50.00"),
        )

    def _add(self, *, product=None, options=None, quantity=1, extra=None):
        payload = {
            "cart_id": str(self.cart.id),
            "product_id": (product or self.product).id,
            "quantity": quantity,
        }
        if options is not None:
            payload["options"] = options
        if extra:
            payload.update(extra)
        return self.client.post(self.add_url, payload, format="json")

    def test_product_with_no_groups_accepts_empty_options(self):
        response = self._add(product=self.plain_product, options=[])
        self.assertEqual(response.status_code, 201)
        item = response.data["cart"]["items"][0]
        self.assertEqual(item["configuration_signature"], "")
        self.assertEqual(item["selected_options"], [])
        self.assertTrue(item["configuration_valid"])
        self.assertFalse(CartItemOption.objects.exists())

    def test_product_with_no_groups_rejects_non_empty_list(self):
        response = self._add(product=self.plain_product, options=[self.lwb.id])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "INVALID_OPTION")
        self.assertFalse(CartItem.objects.exists())

    def test_required_zero_price_option_is_accepted(self):
        response = self._add(options=[self.mwb.id])
        self.assertEqual(response.status_code, 201)
        item = response.data["cart"]["items"][0]
        self.assertEqual(item["options_total"], "0.00")
        self.assertEqual(item["configured_unit_price"], "695.00")
        self.assertEqual(item["price"], "695.00")
        self.assertTrue(item["configuration_valid"])

    def test_paid_option_adds_to_configured_unit_price(self):
        response = self._add(options=[self.lwb.id])
        item = response.data["cart"]["items"][0]
        self.assertEqual(item["base_unit_price"], "695.00")
        self.assertEqual(item["options_total"], "150.00")
        self.assertEqual(item["configured_unit_price"], "845.00")
        self.assertEqual(item["price"], "845.00")

    def test_two_groups_sum_both_adjustments(self):
        response = self._add(options=[self.lwb.id, self.spotlights.id])
        item = response.data["cart"]["items"][0]
        self.assertEqual(item["options_total"], "235.00")
        self.assertEqual(item["configured_unit_price"], "930.00")
        self.assertEqual(item["line_total"], "930.00")

    def test_optional_group_may_be_omitted(self):
        response = self._add(options=[self.lwb.id])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["cart"]["items"][0]["selected_options"]), 1)

    def test_optional_zero_price_none_option_is_accepted(self):
        response = self._add(options=[self.lwb.id, self.no_lights.id])
        self.assertEqual(response.status_code, 201)
        item = response.data["cart"]["items"][0]
        self.assertEqual(item["options_total"], "150.00")
        names = [option["option_name"] for option in item["selected_options"]]
        self.assertEqual(names, ["LWB", "None"])

    def test_required_group_omitted_returns_missing_required_option(self):
        response = self._add(options=[])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "MISSING_REQUIRED_OPTION")
        self.assertEqual(
            response.data["errors"]["options"],
            ["Choose an option for Wheelbase."],
        )
        self.assertFalse(CartItem.objects.exists())

    def test_option_from_another_product_is_rejected(self):
        response = self._add(options=[self.foreign_option.id])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "INVALID_OPTION")

    def test_inactive_option_is_rejected(self):
        self.lwb.active = False
        self.lwb.save(update_fields=["active"])
        response = self._add(options=[self.lwb.id])
        self.assertEqual(response.data["code"], "INVALID_OPTION")

    def test_option_in_inactive_group_is_rejected(self):
        self.lights.active = False
        self.lights.save(update_fields=["active"])
        response = self._add(options=[self.lwb.id, self.spotlights.id])
        self.assertEqual(response.data["code"], "INVALID_OPTION")

    def test_orphan_option_is_rejected(self):
        response = self._add(options=[self.orphan.id])
        self.assertEqual(response.data["code"], "INVALID_OPTION")

    def test_unknown_option_id_is_rejected(self):
        response = self._add(options=[self.lwb.id, 999999])
        self.assertEqual(response.data["code"], "INVALID_OPTION")

    def test_duplicate_option_id_is_rejected(self):
        response = self._add(options=[self.lwb.id, self.lwb.id])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "DUPLICATE_OPTION")

    def test_two_options_in_the_same_group_are_rejected(self):
        response = self._add(options=[self.mwb.id, self.lwb.id])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "DUPLICATE_GROUP_SELECTION")

    def test_invalid_option_id_types_are_rejected(self):
        for value in ("11", 11.5, True, False, None, 0, -1):
            with self.subTest(value=value):
                response = self._add(options=[value])
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "INVALID_OPTIONS_FORMAT")
                self.assertFalse(CartItem.objects.exists())

    def test_browser_price_fields_are_rejected(self):
        response = self._add(
            options=[self.lwb.id],
            extra={"price": "1.00", "total": "1.00"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "UNEXPECTED_PRICE_FIELD")
        self.assertFalse(CartItem.objects.exists())

    def test_same_configuration_increments_one_line(self):
        first = self._add(options=[self.lwb.id, self.spotlights.id])
        second = self._add(options=[self.lwb.id, self.spotlights.id])
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.data["created"])
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 2)
        self.assertEqual(CartItemOption.objects.count(), 2)

    def test_option_order_does_not_split_lines(self):
        self._add(options=[self.lwb.id, self.spotlights.id])
        response = self._add(options=[self.spotlights.id, self.lwb.id])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 2)

    def test_different_option_sets_create_separate_lines(self):
        self._add(options=[self.mwb.id])
        response = self._add(options=[self.lwb.id])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(CartItem.objects.count(), 2)
        totals = sorted(item["line_total"] for item in response.data["cart"]["items"])
        self.assertEqual(totals, ["695.00", "845.00"])
        self.assertEqual(response.data["cart"]["total"], "1540.00")

    def test_cart_item_options_are_created_for_a_new_configured_line(self):
        response = self._add(options=[self.lwb.id, self.no_lights.id])
        item = CartItem.objects.get()
        self.assertEqual(
            set(item.selected_options.values_list("option_id", flat=True)),
            {self.lwb.id, self.no_lights.id},
        )
        self.assertEqual(
            item.configuration_signature,
            f"{self.lwb.id},{self.no_lights.id}"
            if self.lwb.id < self.no_lights.id
            else f"{self.no_lights.id},{self.lwb.id}",
        )
        self.assertEqual(response.data["cart"]["items"][0]["configuration_signature"], item.configuration_signature)

    def test_incrementing_does_not_duplicate_option_rows(self):
        self._add(options=[self.lwb.id])
        self._add(options=[self.lwb.id])
        self.assertEqual(CartItemOption.objects.count(), 1)

    def test_line_total_is_configured_price_times_quantity(self):
        response = self._add(options=[self.lwb.id, self.spotlights.id], quantity=2)
        item = response.data["cart"]["items"][0]
        self.assertEqual(item["configured_unit_price"], "930.00")
        self.assertEqual(item["line_total"], "1860.00")
        self.assertEqual(response.data["cart"]["total"], "1860.00")

    def test_money_fields_are_two_decimal_strings(self):
        item = self._add(options=[self.lwb.id]).data["cart"]["items"][0]
        for field in (
            "base_unit_price",
            "options_total",
            "configured_unit_price",
            "price",
            "line_total",
        ):
            self.assertIsInstance(item[field], str)
            self.assertRegex(item[field], r"^\d+\.\d{2}$")
        self.assertEqual(item["price"], item["configured_unit_price"])
        self.assertRegex(self._add(options=[self.mwb.id]).data["cart"]["total"], r"^\d+\.\d{2}$")

    def test_selected_options_use_catalogue_order_and_shape(self):
        item = self._add(options=[self.spotlights.id, self.lwb.id]).data["cart"]["items"][0]
        selected = item["selected_options"]
        self.assertEqual(
            [option["option_name"] for option in selected],
            ["LWB", "Spotlights"],
        )
        self.assertEqual(
            set(selected[0].keys()),
            {"group_id", "group_name", "option_id", "option_name", "price_adjustment"},
        )
        self.assertEqual(selected[0]["group_name"], "Wheelbase")
        self.assertEqual(selected[0]["price_adjustment"], "150.00")
        self.assertEqual(selected[1]["price_adjustment"], "85.00")

    def test_product_price_change_updates_open_cart(self):
        self._add(options=[self.lwb.id])
        self.product.price = Decimal("800.00")
        self.product.save(update_fields=["price"])
        response = self.client.get(reverse("get_cart"), {"cart_id": str(self.cart.id)})
        item = response.data["cart"]["items"][0]
        self.assertEqual(item["base_unit_price"], "800.00")
        self.assertEqual(item["configured_unit_price"], "950.00")

    def test_option_price_change_updates_open_cart(self):
        self._add(options=[self.lwb.id])
        self.lwb.price_adjustment = Decimal("200.00")
        self.lwb.save(update_fields=["price_adjustment"])
        response = self.client.get(reverse("get_cart"), {"cart_id": str(self.cart.id)})
        item = response.data["cart"]["items"][0]
        self.assertEqual(item["options_total"], "200.00")
        self.assertEqual(item["configured_unit_price"], "895.00")

    def test_deactivated_selected_option_remains_priced_but_invalid(self):
        self._add(options=[self.lwb.id])
        self.lwb.active = False
        self.lwb.save(update_fields=["active"])
        item = self.client.get(
            reverse("get_cart"),
            {"cart_id": str(self.cart.id)},
        ).data["cart"]["items"][0]
        self.assertEqual(item["selected_options"][0]["option_name"], "LWB")
        self.assertEqual(item["options_total"], "150.00")
        self.assertEqual(item["price"], "845.00")
        self.assertFalse(item["configuration_valid"])

    def test_stored_blank_line_missing_required_group_is_invalid(self):
        item = CartItem.objects.create(
            cart=self.cart,
            product=self.product,
            quantity=2,
            configuration_signature="",
        )
        updated_at = item.updated_at

        response = self.client.get(
            reverse("get_cart"),
            {"cart_id": str(self.cart.id)},
        )
        payload = response.data["cart"]["items"][0]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["configuration_signature"], "")
        self.assertEqual(payload["selected_options"], [])
        self.assertEqual(payload["price"], "695.00")
        self.assertEqual(payload["quantity"], 2)
        self.assertFalse(payload["configuration_valid"])

        item.refresh_from_db()
        self.assertEqual(item.updated_at, updated_at)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.configuration_signature, "")
        self.assertFalse(item.selected_options.exists())

        removed = self.client.delete(
            reverse("remove_from_cart"),
            {"cart_id": str(self.cart.id), "item_id": item.id},
            format="json",
        )
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(CartItem.objects.filter(id=item.id).exists())

    def test_signature_mismatch_marks_line_invalid(self):
        item = CartItem.objects.create(
            cart=self.cart,
            product=self.product,
            quantity=1,
            configuration_signature=str(self.lwb.id),
        )
        CartItemOption.objects.create(
            cart_item=item,
            group=self.wheelbase,
            option=self.mwb,
        )
        payload = self.client.get(
            reverse("get_cart"),
            {"cart_id": str(self.cart.id)},
        ).data["cart"]["items"][0]
        self.assertFalse(payload["configuration_valid"])

    def test_signature_mismatch_on_increment_returns_conflict(self):
        CartItem.objects.create(
            cart=self.cart,
            product=self.product,
            quantity=1,
            configuration_signature=str(self.lwb.id),
        )
        response = self._add(options=[self.lwb.id])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "CONFIGURATION_CONFLICT")
        self.assertEqual(CartItem.objects.get().quantity, 1)

    def test_integrity_error_without_winner_returns_conflict(self):
        existing = self._add(options=[self.mwb.id])
        self.assertEqual(existing.status_code, 201)
        other_line = CartItem.objects.get()
        other_quantity = other_line.quantity
        other_option_ids = list(
            CartItemOption.objects.values_list("option_id", flat=True)
        )

        with patch.object(
            CartItemOption.objects,
            "bulk_create",
            side_effect=IntegrityError("forced uniqueness conflict"),
        ):
            response = self._add(options=[self.lwb.id])

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "CONFIGURATION_CONFLICT")
        self.assertEqual(
            response.data["error"],
            "This cart line could not be updated. Remove it and add the product again.",
        )
        self.assertEqual(CartItem.objects.count(), 1)
        other_line.refresh_from_db()
        self.assertEqual(other_line.id, CartItem.objects.get().id)
        self.assertEqual(other_line.quantity, other_quantity)
        self.assertEqual(
            list(CartItemOption.objects.values_list("option_id", flat=True)),
            other_option_ids,
        )
        self.assertFalse(
            CartItem.objects.filter(
                configuration_signature=str(self.lwb.id)
            ).exists()
        )

    def test_uniqueness_race_with_winner_increments_once(self):
        first = self._add(options=[self.lwb.id])
        self.assertEqual(first.status_code, 201)
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 1)
        self.assertEqual(CartItemOption.objects.count(), 1)

        real_select_for_update = CartItem.objects.select_for_update
        lookups = {"count": 0}

        def miss_then_lock(*args, **kwargs):
            queryset = real_select_for_update(*args, **kwargs)
            lookups["count"] += 1
            if lookups["count"] == 1:
                return queryset.none()
            return queryset

        with patch.object(
            CartItem.objects,
            "select_for_update",
            side_effect=miss_then_lock,
        ):
            response = self._add(options=[self.lwb.id])

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["created"])
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 2)
        self.assertEqual(CartItemOption.objects.count(), 1)
        self.assertEqual(
            list(CartItemOption.objects.values_list("option_id", flat=True)),
            [self.lwb.id],
        )

    def test_update_quantity_returns_configured_totals(self):
        added = self._add(options=[self.lwb.id, self.spotlights.id])
        item_id = added.data["cart"]["items"][0]["item_id"]
        response = self.client.patch(
            reverse("update_cart_item"),
            {
                "cart_id": str(self.cart.id),
                "item_id": item_id,
                "quantity": 3,
            },
            format="json",
        )
        item = response.data["cart"]["items"][0]
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["configured_unit_price"], "930.00")
        self.assertEqual(item["line_total"], "2790.00")
        self.assertEqual(response.data["cart"]["total"], "2790.00")

    def test_remove_cascades_option_rows(self):
        added = self._add(options=[self.lwb.id])
        item_id = added.data["cart"]["items"][0]["item_id"]
        self.client.delete(
            reverse("remove_from_cart"),
            {"cart_id": str(self.cart.id), "item_id": item_id},
            format="json",
        )
        self.assertFalse(CartItem.objects.exists())
        self.assertFalse(CartItemOption.objects.exists())

    def test_clear_removes_configured_lines_and_option_rows(self):
        self._add(options=[self.lwb.id])
        self._add(options=[self.mwb.id, self.spotlights.id])
        response = self.client.delete(
            reverse("clear_cart"),
            {"cart_id": str(self.cart.id)},
            format="json",
        )
        self.assertEqual(response.data["cart"]["items"], [])
        self.assertFalse(CartItem.objects.exists())
        self.assertFalse(CartItemOption.objects.exists())

    def test_other_cart_cannot_update_item(self):
        added = self._add(options=[self.lwb.id])
        item_id = added.data["cart"]["items"][0]["item_id"]
        other = Cart.objects.create(session_key="other-pricing")
        response = self.client.patch(
            reverse("update_cart_item"),
            {"cart_id": str(other.id), "item_id": item_id, "quantity": 9},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(CartItem.objects.get().quantity, 1)

    def test_empty_options_object_is_rejected(self):
        response = self._add(options={})
        self._assert_invalid_options_object(response)
        self.assertFalse(CartItem.objects.exists())
        self.assertFalse(CartItemOption.objects.exists())

    def test_populated_legacy_object_is_rejected(self):
        response = self._add(
            options={"3": 11, "4": [self.lwb.id], "5": None},
        )
        self._assert_invalid_options_object(response)
        self.assertFalse(CartItem.objects.exists())
        self.assertFalse(CartItemOption.objects.exists())

    def test_rejected_object_does_not_increment_existing_lines(self):
        configured = self._add(options=[self.lwb.id])
        blank = self._add(product=self.plain_product, options=[])
        self.assertEqual(configured.status_code, 201)
        self.assertEqual(blank.status_code, 201)
        configured_item = CartItem.objects.get(product=self.product)
        blank_item = CartItem.objects.get(product=self.plain_product)
        option_count = CartItemOption.objects.count()

        for payload in ({}, {"3": 11, "4": [20], "5": None}):
            with self.subTest(options=payload):
                against_configured = self._add(options=payload)
                against_blank = self._add(product=self.plain_product, options=payload)
                self._assert_invalid_options_object(against_configured)
                self._assert_invalid_options_object(against_blank)

        configured_item.refresh_from_db()
        blank_item.refresh_from_db()
        self.assertEqual(CartItem.objects.count(), 2)
        self.assertEqual(configured_item.quantity, 1)
        self.assertEqual(blank_item.quantity, 1)
        self.assertEqual(CartItemOption.objects.count(), option_count)

    def _assert_invalid_options_object(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["success"], False)
        self.assertEqual(response.data["code"], "INVALID_OPTIONS_FORMAT")
        self.assertEqual(
            response.data["error"],
            "Your product selections could not be read. "
            "Refresh the product page and select your options again.",
        )

    def test_invalid_options_type_is_rejected(self):
        response = self._add(extra={"options": "11,20"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "INVALID_OPTIONS_FORMAT")

    def test_get_cart_does_not_query_per_option(self):
        self._add(options=[self.lwb.id, self.spotlights.id])
        self._add(options=[self.mwb.id])
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(
                reverse("get_cart"),
                {"cart_id": str(self.cart.id)},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["cart"]["items"]), 2)
        self.assertLessEqual(len(captured), 8)
