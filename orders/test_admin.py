from decimal import Decimal
from html.parser import HTMLParser

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from .admin import (
    OrderAdmin,
    OrderItemAdmin,
    OrderItemInline,
    OrderItemOptionAdmin,
    OrderItemOptionInline,
)
from .factories import create_snapshot_order
from .models import Order, OrderItem, OrderItemOption


class OrderAdminInspectionTests(TestCase):
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
        self.order, _catalogue = create_snapshot_order()

    def test_order_admin_is_inspection_only_except_internal_notes(self):
        order_admin = OrderAdmin(Order, self.site)
        self.assertFalse(order_admin.has_add_permission(self.request))
        self.assertFalse(order_admin.has_delete_permission(self.request, self.order))
        self.assertTrue(order_admin.has_change_permission(self.request, self.order))
        self.assertIn("internal_notes", order_admin.fields)
        self.assertNotIn("internal_notes", order_admin.readonly_fields)
        for field in (
            "reference",
            "customer_name",
            "full_total",
            "deposit_required",
            "balance_on_completion",
            "amount_paid",
            "outstanding_balance",
            "payment_status",
            "job_status",
            "is_finalised",
        ):
            self.assertIn(field, order_admin.readonly_fields)
        self.assertEqual(
            list(order_admin.list_display),
            [
                "reference",
                "customer_name",
                "created_at",
                "full_total",
                "deposit_required",
                "balance_on_completion",
                "amount_paid",
                "outstanding_balance",
                "payment_status",
                "job_status",
                "is_finalised",
            ],
        )
        self.assertNotIn("delete_selected", order_admin.get_actions(self.request))
        self.assertFalse(hasattr(order_admin, "mark_paid"))
        self.assertNotIn("mark_paid", order_admin.get_actions(self.request))

    def test_item_and_option_admins_cannot_mutate_snapshots(self):
        item_admin = OrderItemAdmin(OrderItem, self.site)
        option_admin = OrderItemOptionAdmin(OrderItemOption, self.site)
        item_inline = OrderItemInline(Order, self.site)
        option_inline = OrderItemOptionInline(OrderItem, self.site)

        for admin_obj in (item_admin, option_admin):
            self.assertFalse(admin_obj.has_add_permission(self.request))
            self.assertFalse(admin_obj.has_change_permission(self.request))
            self.assertFalse(admin_obj.has_delete_permission(self.request))

        for inline in (item_inline, option_inline):
            self.assertEqual(inline.extra, 0)
            self.assertEqual(inline.max_num, 0)
            self.assertFalse(inline.can_delete)
            self.assertFalse(inline.has_add_permission(self.request))
            self.assertFalse(inline.has_change_permission(self.request))
            self.assertFalse(inline.has_delete_permission(self.request))
        self.assertEqual(item_inline.readonly_fields, item_inline.fields)
        self.assertEqual(option_inline.readonly_fields, option_inline.fields)

    @override_settings(
        STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage"
    )
    def test_admin_post_cannot_change_financial_or_status_fields(self):
        self.client.force_login(self.user)
        url = reverse("admin:orders_order_change", args=[self.order.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        payload = _admin_form_values(page.content.decode())
        payload.update(
            {
                "internal_notes": "Checked in workshop",
                "full_total": "1.00",
                "deposit_required": "1.00",
                "balance_on_completion": "0.00",
                "amount_paid": "50.00",
                "payment_status": "paid",
                "job_status": "fitted",
                "is_finalised": "False",
                "customer_name": "Someone else",
                "_save": "Save",
            }
        )
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.internal_notes, "Checked in workshop")
        self.assertEqual(self.order.full_total, Decimal("825.00"))
        self.assertEqual(self.order.amount_paid, Decimal("0.00"))
        self.assertEqual(self.order.payment_status, Order.PAYMENT_PENDING_DEPOSIT)
        self.assertEqual(self.order.job_status, Order.JOB_NEW)
        self.assertTrue(self.order.is_finalised)
        self.assertEqual(self.order.customer_name, "Alex Farmer")

    def test_non_staff_cannot_open_an_order(self):
        outsider = User.objects.create_user(
            "viewer",
            "viewer@example.com",
            "password",
        )
        self.client.force_login(outsider)
        url = reverse("admin:orders_order_change", args=[self.order.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)
        self.order.refresh_from_db()
        self.assertEqual(self.order.full_total, Decimal("825.00"))
        self.assertEqual(self.order.internal_notes, "")


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
