from django.shortcuts import render, get_object_or_404
from .models import Product, Category, Review
from rest_framework import generics
from .serializers import (
    ProductSerializer,
    ProductListSerializer,
    CategorySerializer,
    CategoryWithProductsSerializer,
    ReviewSerializer,
)


def categories(request):
    return {
        'categories': Category.objects.all()
    }

def get_products(request):
    products = Product.objects.all()
    return render(request, 'store/index.html', {'products': products})

def category_list(request, category_slug=None):
    category = get_object_or_404(Category, slug=category_slug)
    products = Product.objects.filter(category=category)
    return render(request, 'store/products/category.html', {'category': category, 'products': products})


def product_detail(request, slug):
    product = get_object_or_404(Product, slug=slug)
    images = product.productimage_set.all()
    context = {
        'product': product,
        'images': images
    }
    return render(request, 'store/products/single.html', context)


def about(request):
    return render(request, 'store/about.html')


class ProductListAPI(generics.ListAPIView):
    serializer_class = ProductListSerializer

    def get_queryset(self):
        queryset = Product.objects.all().prefetch_related("productimage_set")
        category_id = self.request.query_params.get("category_id")
        if category_id:
            queryset = queryset.filter(category_id=category_id)
        return queryset

class ProductDetailAPI(generics.RetrieveAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    lookup_field = 'slug'

class CategoryListAPI(generics.ListAPIView):
    serializer_class = CategorySerializer

    def get_queryset(self):
        category_type = self.request.query_params.get('type', 'product')
        return Category.objects.filter(type=category_type)

class CategoryDetailAPI(generics.RetrieveAPIView):
    queryset = Category.objects.all()
    serializer_class = CategoryWithProductsSerializer
    lookup_field = 'slug'

class ReviewListAPI(generics.ListAPIView):
    queryset = Review.objects.all()
    serializer_class = ReviewSerializer