from django.conf import settings
from django.contrib import admin

from .models import Order, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    readonly_fields = (
        "product_id",
        "product_name",
        "sku",
        "quantity",
        "unit_price",
        "line_total",
    )


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    @admin.display(description="Balance payment URL")
    def balance_payment_url(self, obj):
        if not obj or not obj.balance_token:
            return ""
        return f"{settings.SITE_URL.rstrip('/')}/balance/{obj.balance_token}"

    list_display = (
        "id",
        "customer_name",
        "customer_email",
        "status",
        "total_amount",
        "deposit_amount",
        "balance_amount",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("id", "customer_name", "customer_email")
    readonly_fields = (
        "id",
        "balance_token",
        "created_at",
        "updated_at",
        "deposit_paid_at",
        "balance_paid_at",
        "stripe_deposit_session_id",
        "stripe_deposit_payment_intent_id",
        "stripe_balance_session_id",
        "balance_payment_url",
    )
    inlines = [OrderItemInline]


admin.site.register(OrderItem)
