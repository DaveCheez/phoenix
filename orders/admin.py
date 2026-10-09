from django.contrib import admin

from .models import Order, OrderItem, OrderItemOption


class OrderItemOptionInline(admin.TabularInline):
    model = OrderItemOption
    extra = 0
    max_num = 0
    can_delete = False
    fields = [
        "position",
        "group_name",
        "option_name",
        "option_sku",
        "price_adjustment",
        "original_group_id",
        "original_option_id",
    ]
    readonly_fields = fields
    ordering = ("position", "id")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    max_num = 0
    can_delete = False
    fields = [
        "position",
        "product_name",
        "product_slug",
        "sku",
        "configuration_signature",
        "quantity",
        "base_unit_price",
        "options_total",
        "configured_unit_price",
        "line_total",
    ]
    readonly_fields = fields
    ordering = ("position", "id")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = [
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
    ]
    list_filter = ["payment_status", "job_status", "created_at"]
    search_fields = ["reference", "customer_name", "customer_email"]
    ordering = ("-created_at", "-id")
    inlines = [OrderItemInline]
    readonly_fields = [
        "id",
        "reference",
        "created_at",
        "updated_at",
        "customer_name",
        "customer_email",
        "customer_phone",
        "vehicle_make",
        "vehicle_model",
        "vehicle_year",
        "vehicle_wheelbase",
        "vehicle_registration",
        "customer_notes",
        "currency",
        "full_total",
        "deposit_required",
        "balance_on_completion",
        "amount_paid",
        "outstanding_balance",
        "fulfilment_method",
        "fitting_charge",
        "tax_treatment",
        "tax_amount",
        "payment_terms",
        "payment_terms_text",
        "payment_status",
        "job_status",
        "is_finalised",
        "source_cart",
    ]
    fields = readonly_fields + ["internal_notes"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "order",
        "position",
        "product_name",
        "quantity",
        "configured_unit_price",
        "line_total",
    ]
    readonly_fields = [
        "order",
        "position",
        "original_product_id",
        "product",
        "product_name",
        "product_slug",
        "sku",
        "configuration_signature",
        "quantity",
        "base_unit_price",
        "options_total",
        "configured_unit_price",
        "line_total",
    ]
    fields = readonly_fields
    inlines = [OrderItemOptionInline]
    search_fields = ["product_name", "order__reference"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrderItemOption)
class OrderItemOptionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "order_item",
        "position",
        "group_name",
        "option_name",
        "price_adjustment",
    ]
    readonly_fields = [
        "order_item",
        "position",
        "original_group_id",
        "original_option_id",
        "group",
        "option",
        "group_name",
        "option_name",
        "option_sku",
        "price_adjustment",
    ]
    fields = readonly_fields
    search_fields = ["option_name", "group_name", "order_item__order__reference"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
