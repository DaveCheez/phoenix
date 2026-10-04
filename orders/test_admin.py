from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

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
