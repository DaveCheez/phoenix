from django.urls import path
from .views import (
    ProductListAPI,
    ProductDetailAPI,
    CategoryListAPI,
    CategoryDetailAPI,
    ReviewListAPI
)

urlpatterns = [
    path('products/', ProductListAPI.as_view(), name='product-list-api'),
    path('products/<slug:slug>/', ProductDetailAPI.as_view(), name='product-detail-api'),
    path('categories/', CategoryListAPI.as_view(), name='category-list-api'),
    path('category/<slug:slug>/', CategoryDetailAPI.as_view(), name='category-detail-api'),
    path('reviews/', ReviewListAPI.as_view(), name='review-list-api'),
]
