from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from rest_framework import generics

from .models import Category, HomeSlide, Product, Review
from .serializers import (
    CategorySerializer,
    CategoryWithProductsSerializer,
    HomeSlideSerializer,
    ProductListSerializer,
    ProductSerializer,
    ReviewSerializer,
)


def categories(request):
    return {"categories": Category.objects.all()}


def get_products(request):
    products = Product.objects.all()
    return render(request, "store/index.html", {"products": products})


def category_list(request, category_slug=None):
    category = get_object_or_404(Category, slug=category_slug)
    products = Product.objects.filter(category=category)
    return render(
        request,
        "store/products/category.html",
        {"category": category, "products": products},
    )


def product_detail(request, slug):
    product = get_object_or_404(Product, slug=slug)
    images = product.productimage_set.all()
    return render(
        request,
        "store/products/single.html",
        {"product": product, "images": images},
    )


def about(request):
    return render(request, "store/about.html")


class ProductListAPI(generics.ListAPIView):
    serializer_class = ProductListSerializer

    def get_queryset(self):
        queryset = Product.objects.all().prefetch_related("productimage_set")
        category_id = self.request.query_params.get("category_id")
        if category_id:
            queryset = queryset.filter(category_id=category_id)
        return queryset


class ProductDetailAPI(generics.RetrieveAPIView):
    queryset = Product.objects.prefetch_related(
        "productimage_set",
        "option_groups__options",
    )
    serializer_class = ProductSerializer
    lookup_field = "slug"


class CategoryListAPI(generics.ListAPIView):
    serializer_class = CategorySerializer

    def get_queryset(self):
        category_type = self.request.query_params.get("type", "product")
        return Category.objects.filter(type=category_type).prefetch_related(
            "categoryimage_set"
        )


class CategoryDetailAPI(generics.RetrieveAPIView):
    queryset = Category.objects.prefetch_related(
        "categoryimage_set",
        "store__productimage_set",
    )
    serializer_class = CategoryWithProductsSerializer
    lookup_field = "slug"


class ReviewListAPI(generics.ListAPIView):
    queryset = Review.objects.all()
    serializer_class = ReviewSerializer


class HomeSlideListAPI(generics.ListAPIView):
    serializer_class = HomeSlideSerializer

    def get_queryset(self):
        now = timezone.now()
        return (
            HomeSlide.objects.filter(is_active=True)
            .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=now))
            .order_by("display_order", "id")
        )
