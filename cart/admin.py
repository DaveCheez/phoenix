from django.contrib import admin

from .models import Cart, CartItem, CartItemOption


class CartItemOptionInline(admin.TabularInline):
    model = CartItemOption
    extra = 0
    max_num = 0
    can_delete = False
    fields = ["group", "option", "created_at"]
    readonly_fields = ["group", "option", "created_at"]
    ordering = ("group_id", "option_id", "id")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "created_at"]


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "cart",
        "product",
        "quantity",
        "configuration_signature",
        "created_at",
        "updated_at",
    ]
    readonly_fields = [
        "cart",
        "product",
        "configuration_signature",
        "created_at",
        "updated_at",
    ]
    fields = [
        "cart",
        "product",
        "quantity",
        "configuration_signature",
        "created_at",
        "updated_at",
    ]
    list_filter = ["product"]
    search_fields = ["product__name", "configuration_signature"]
    inlines = [CartItemOptionInline]

    def has_add_permission(self, request):
        return False


@admin.register(CartItemOption)
class CartItemOptionAdmin(admin.ModelAdmin):
    list_display = ["id", "cart_item", "group", "option", "created_at"]
    readonly_fields = ["cart_item", "group", "option", "created_at"]
    fields = ["cart_item", "group", "option", "created_at"]
    list_filter = ["group__product"]
    search_fields = ["option__name", "group__name"]
    ordering = ("group_id", "option_id", "id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
