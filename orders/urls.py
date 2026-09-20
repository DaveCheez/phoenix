from django.urls import path

from . import views

urlpatterns = [
    path("deposit/checkout/", views.create_deposit_checkout, name="deposit-checkout"),
    path("<uuid:order_id>/status/", views.order_status, name="order-status"),
    path("balance/<uuid:token>/", views.balance_details, name="balance-details"),
    path("balance/<uuid:token>/checkout/", views.create_balance_checkout, name="balance-checkout"),
    path("stripe/webhook/", views.stripe_webhook, name="stripe-webhook"),
]
