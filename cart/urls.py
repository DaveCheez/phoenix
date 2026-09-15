# cart/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('', views.get_cart),         # GET /api/cart/
    path('create/', views.create_cart, name='create_cart'),
    path('add/', views.add_to_cart),            # POST /api/cart/add/
    path('update/', views.update_cart_item),    # POST /api/cart/update/
    path('remove/', views.remove_from_cart),    # POST /api/cart/remove/
]