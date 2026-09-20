from decimal import Decimal

import stripe
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from cart.models import Cart, CartItem

from .models import Order, OrderItem


def _money(value):
    return Decimal(value).quantize(Decimal("0.01"))


def _stripe_key():
    key = getattr(settings, "STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    if not key.startswith(("sk_test_", "sk_live_")):
        raise RuntimeError("STRIPE_SECRET_KEY is invalid")
    stripe.api_key = key
    return key


def _domain(request):
    return getattr(settings, "SITE_URL", "").rstrip("/") or request.build_absolute_uri("/").rstrip("/")


def _build_order(cart, customer):
    items = list(CartItem.objects.select_related("product").filter(cart=cart))
    if not items:
        raise ValueError("Your cart is empty")

    total = Decimal("0.00")
    snapshots = []
    for item in items:
        unit = _money(item.product.price or 0)
        line = _money(unit * item.quantity)
        total += line
        snapshots.append({
            "product_id": item.product_id,
            "product_name": item.product.name,
            "sku": getattr(item.product, "sku", "") or "",
            "quantity": item.quantity,
            "unit_price": unit,
            "line_total": line,
        })

    total = _money(total)
    deposit = Order.calculate_deposit(total)
    balance = _money(total - deposit)

    order = Order.objects.create(
        cart=cart,
        customer_name=customer["name"],
        customer_email=customer["email"],
        address_line1=customer.get("line1", ""),
        address_city=customer.get("city", ""),
        address_postcode=customer.get("postcode", ""),
        total_amount=total,
        deposit_amount=deposit,
        balance_amount=balance,
    )
    OrderItem.objects.bulk_create([
        OrderItem(order=order, **snapshot) for snapshot in snapshots
    ])
    return order


@api_view(["POST"])
@permission_classes([AllowAny])
def create_deposit_checkout(request):
    data = request.data or {}
    cart_id = data.get("cart_id")
    customer = data.get("customer") or {}

    required = ["name", "email"]
    missing = [field for field in required if not str(customer.get(field, "")).strip()]
    if missing:
        return Response({"success": False, "error": "Customer name and email are required.", "code": "CUSTOMER_REQUIRED"}, status=400)
    if not cart_id:
        return Response({"success": False, "error": "Missing cart ID.", "code": "MISSING_CART_ID"}, status=400)

    try:
        with transaction.atomic():
            cart = Cart.objects.get(id=cart_id)
            order = _build_order(cart, {
                "name": str(customer["name"]).strip(),
                "email": str(customer["email"]).strip(),
                "line1": str(customer.get("line1", "")).strip(),
                "city": str(customer.get("city", "")).strip(),
                "postcode": str(customer.get("postcode", "")).strip(),
            })

        _stripe_key()
        session = stripe.checkout.Session.create(
            mode="payment",
            success_url=f"{_domain(request)}/order-confirmation?order={order.id}&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{_domain(request)}/checkout?cancelled=1",
            customer_email=order.customer_email,
            client_reference_id=str(order.id),
            metadata={
                "order_id": str(order.id),
                "payment_stage": "deposit",
            },
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "unit_amount": int(order.deposit_amount * 100),
                    "product_data": {
                        "name": f"Phoenix Vanz deposit - order {str(order.id)[:8].upper()}",
                        "description": f"1/3 deposit. Balance of £{order.balance_amount:.2f} due on completion.",
                    },
                },
                "quantity": 1,
            }],
            custom_text={
                "submit": {
                    "message": "This payment is a 1/3 deposit. The remaining balance is due on completion.",
                },
            },
        )
        order.stripe_deposit_session_id = session.id
        order.save(update_fields=["stripe_deposit_session_id", "updated_at"])

        return Response({
            "success": True,
            "order_id": str(order.id),
            "total": f"{order.total_amount:.2f}",
            "deposit": f"{order.deposit_amount:.2f}",
            "balance": f"{order.balance_amount:.2f}",
            "checkout_url": session.url,
        })
    except Cart.DoesNotExist:
        return Response({"success": False, "error": "Cart not found.", "code": "CART_NOT_FOUND"}, status=404)
    except ValueError as exc:
        return Response({"success": False, "error": str(exc), "code": "ORDER_INVALID"}, status=400)
    except stripe.error.StripeError as exc:
        return Response({"success": False, "error": "Stripe could not create the deposit payment.", "code": "STRIPE_ERROR"}, status=502)
    except Exception:
        return Response({"success": False, "error": "Unable to create the deposit checkout.", "code": "CHECKOUT_ERROR"}, status=500)


@api_view(["GET"])
@permission_classes([AllowAny])
def order_status(request, order_id):
    session_id = str(request.query_params.get("session_id", "")).strip()
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return Response({"success": False, "error": "Order not found."}, status=404)

    if session_id and session_id != order.stripe_deposit_session_id:
        return Response({"success": False, "error": "Order session does not match."}, status=403)

    return Response({
        "success": True,
        "order": {
            "id": str(order.id),
            "status": order.status,
            "total": f"{order.total_amount:.2f}",
            "deposit": f"{order.deposit_amount:.2f}",
            "balance": f"{order.balance_amount:.2f}",
            "deposit_paid": bool(order.deposit_paid_at),
        },
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def balance_details(request, token):
    try:
        order = Order.objects.get(balance_token=token)
    except Order.DoesNotExist:
        return Response({"success": False, "error": "Balance payment link not found."}, status=404)
    return Response({
        "success": True,
        "order": {
            "id": str(order.id),
            "status": order.status,
            "customer_name": order.customer_name,
            "total": f"{order.total_amount:.2f}",
            "deposit_paid": f"{order.deposit_amount:.2f}" if order.deposit_paid_at else "0.00",
            "balance": f"{order.balance_amount:.2f}",
        },
    })


@api_view(["POST"])
@permission_classes([AllowAny])
def create_balance_checkout(request, token):
    try:
        order = Order.objects.get(balance_token=token)
    except Order.DoesNotExist:
        return Response({"success": False, "error": "Balance payment link not found."}, status=404)

    if order.status not in {"deposit_paid", "balance_due"}:
        return Response({"success": False, "error": "This order is not currently awaiting its balance."}, status=400)
    if order.balance_amount <= 0:
        return Response({"success": False, "error": "There is no balance due on this order."}, status=400)

    try:
        _stripe_key()
        session = stripe.checkout.Session.create(
            mode="payment",
            success_url=f"{_domain(request)}/balance/{order.balance_token}?paid=1&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{_domain(request)}/balance/{order.balance_token}?cancelled=1",
            customer_email=order.customer_email,
            client_reference_id=str(order.id),
            metadata={
                "order_id": str(order.id),
                "payment_stage": "balance",
            },
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "unit_amount": int(order.balance_amount * 100),
                    "product_data": {
                        "name": f"Phoenix Vanz completion balance - order {str(order.id)[:8].upper()}",
                    },
                },
                "quantity": 1,
            }],
        )
        order.stripe_balance_session_id = session.id
        order.save(update_fields=["stripe_balance_session_id", "updated_at"])
        return Response({"success": True, "checkout_url": session.url, "amount": f"{order.balance_amount:.2f}"})
    except stripe.error.StripeError:
        return Response({"success": False, "error": "Stripe could not create the balance payment."}, status=502)


@api_view(["POST"])
@permission_classes([AllowAny])
def stripe_webhook(request):
    secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        return HttpResponse("Webhook signing secret not configured", status=500)
    signature = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        event = stripe.Webhook.construct_event(request.body, signature, secret)
    except ValueError:
        return HttpResponse("Invalid payload", status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse("Invalid signature", status=400)

    event_type = event["type"]
    data = event["data"]["object"]
    metadata = data.get("metadata", {}) or {}
    order_id = metadata.get("order_id")
    stage = metadata.get("payment_stage")

    if order_id and event_type in {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    }:
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return HttpResponse("Order not found", status=200)

        if stage == "deposit" and data.get("payment_status") == "paid":
            if order.status in {"pending_deposit", "deposit_paid"}:
                order.status = "balance_due"
                order.deposit_paid_at = order.deposit_paid_at or timezone.now()
                order.stripe_deposit_payment_intent_id = data.get("payment_intent") or order.stripe_deposit_payment_intent_id
                order.save(update_fields=["status", "deposit_paid_at", "stripe_deposit_payment_intent_id", "updated_at"])
                if order.cart_id:
                    CartItem.objects.filter(cart_id=order.cart_id).delete()
        elif stage == "balance":
            order.status = "paid"
            order.balance_paid_at = order.balance_paid_at or timezone.now()
            order.save(update_fields=["status", "balance_paid_at", "updated_at"])

    return HttpResponse("ok", status=200)
