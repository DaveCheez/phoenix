from django.urls import path

from . import views


urlpatterns = [
    path("", views.get_cart, name="get_cart"),
    path("create/", views.create_cart, name="create_cart"),
    path("add/", views.add_to_cart, name="add_to_cart"),
    path("update/", views.update_cart_item, name="update_cart_item"),
    path("remove/", views.remove_from_cart, name="remove_from_cart"),
    path("clear/", views.clear_cart, name="clear_cart"),
]
