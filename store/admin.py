import nested_admin
from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils.html import format_html
from nested_admin.formsets import NestedInlineFormSet

from .models import (
    Product, Category, ProductImage, CategoryImage,
    ProductOption, ProductOptionGroup, Review, HomeSlide
)

# --- INLINE ADMIN CLASSES ---

class ProductOptionInlineFormSet(NestedInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        default_count = 0
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            if form.cleaned_data.get("is_default"):
                default_count += 1
        if default_count > 1:
            raise ValidationError("Only one default option is allowed per group.")


class ProductOptionInline(nested_admin.NestedTabularInline):
    model = ProductOption
    extra = 1
    formset = ProductOptionInlineFormSet
    fields = [
        "name",
        "price_adjustment",
        "sku",
        "is_default",
        "display_order",
        "active",
    ]


class ProductOptionGroupInline(nested_admin.NestedStackedInline):
    model = ProductOptionGroup
    inlines = [ProductOptionInline]
    extra = 1
    fields = ["name", "required", "active", "display_order", "help_text"]


class ProductImageInline(nested_admin.NestedTabularInline):
    model = ProductImage
    extra = 1


class CategoryImageInline(nested_admin.NestedTabularInline):
    model = CategoryImage
    extra = 1


# --- MAIN ADMIN REGISTRATIONS ---

@admin.register(Product)
class ProductAdmin(nested_admin.NestedModelAdmin):
    list_display = ['name', 'slug', 'price', 'created_at', 'updated_at']
    prepopulated_fields = {'slug': ('name',)}
    list_filter = ['category', 'created_at']
    search_fields = ['name', 'description']
    inlines = [ProductImageInline, ProductOptionGroupInline]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}
    inlines = [CategoryImageInline]
    search_fields = ['name']


@admin.register(ProductOptionGroup)
class ProductOptionGroupAdmin(admin.ModelAdmin):
    list_display = ["name", "product", "required", "active", "display_order"]
    list_filter = ["required", "active", "product"]
    search_fields = ["name", "product__name"]
    ordering = ["display_order", "id"]
    fields = ["product", "name", "required", "active", "display_order", "help_text"]


class ProductOptionAdminForm(forms.ModelForm):
    class Meta:
        model = ProductOption
        fields = [
            "group",
            "name",
            "price_adjustment",
            "sku",
            "is_default",
            "display_order",
            "active",
        ]


@admin.register(ProductOption)
class ProductOptionAdmin(admin.ModelAdmin):
    form = ProductOptionAdminForm
    list_display = [
        "name",
        "group",
        "price_adjustment",
        "is_default",
        "active",
        "display_order",
    ]
    list_filter = ["active", "is_default", "group__product"]
    search_fields = ["name", "sku", "group__name"]
    ordering = ["display_order", "id"]
    fields = [
        "group",
        "name",
        "price_adjustment",
        "sku",
        "is_default",
        "display_order",
        "active",
    ]


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'image_preview', 'created_at', 'updated_at']
    prepopulated_fields = {'slug': ('name',)}

    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" width="50" style="object-fit:cover;" />', obj.image.url)
        return "-"
    image_preview.short_description = 'Preview'


@admin.register(CategoryImage)
class CategoryImageAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'image_type', 'image_preview']
    prepopulated_fields = {'slug': ('name',)}

    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" width="50" style="object-fit:cover;" />', obj.image.url)
        return "-"
    image_preview.short_description = 'Preview'

@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at']
    search_fields = ['name', 'content']
    ordering = ['-created_at']

@admin.register(HomeSlide)
class HomeSlideAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "display_order",
        "is_active",
        "starts_at",
        "ends_at",
        "image_preview",
    ]
    list_editable = ["display_order", "is_active"]
    list_filter = ["is_active", "starts_at", "ends_at"]
    search_fields = ["title", "subtitle"]
    ordering = ["display_order", "id"]

    fieldsets = (
        (None, {"fields": ("title", "subtitle", "image", "mobile_image")}),
        (
            "Call to action",
            {"fields": ("button_text", "button_url")},
        ),
        (
            "Publishing",
            {"fields": ("display_order", "is_active", "starts_at", "ends_at")},
        ),
    )

    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" width="90" height="50" style="object-fit:cover;border-radius:4px;" />',
                obj.image.url,
            )
        return "-"

    image_preview.short_description = "Preview"
