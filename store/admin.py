import nested_admin
from django.utils.html import format_html
from django.contrib import admin
from .models import (
    Product, Category, ProductImage, CategoryImage,
    ProductOption, ProductOptionGroup, Review
)

# --- INLINE ADMIN CLASSES ---

class ProductOptionInline(nested_admin.NestedTabularInline):
    model = ProductOption
    extra = 1


class ProductOptionGroupInline(nested_admin.NestedStackedInline):
    model = ProductOptionGroup
    inlines = [ProductOptionInline]
    extra = 1


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
    list_display = ['name', 'product', 'required']


@admin.register(ProductOption)
class ProductOptionAdmin(admin.ModelAdmin):
    list_display = ['name', 'group', 'price']


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