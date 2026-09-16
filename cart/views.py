from django.core.exceptions import ValidationError
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from store.models import Product

from .models import Cart, CartItem
from .services import cart_payload


MAX_CART_QUANTITY = 999


def _integer(value, *, field_name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a whole number")

    if parsed < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    if parsed > maximum:
        raise ValueError(f"{field_name} cannot be greater than {maximum}")
    return parsed


def _cart_by_id(cart_id, *, lock: bool = False) -> Cart:
    queryset = Cart.objects
    if lock:
        queryset = queryset.select_for_update()
    return queryset.get(id=cart_id)


def _cart_error(exc) -> Response:
    if isinstance(exc, (Cart.DoesNotExist, ValidationError, ValueError)):
        return Response(
            {"success": False, "code": "CART_NOT_FOUND", "error": "Cart not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    raise exc


@api_view(["POST"])
def create_cart(request):
    """Create or reuse the anonymous cart associated with this Django session."""
    if not request.session.session_key:
        request.session.create()

    session_key = request.session.session_key
    cart = Cart.objects.filter(session_key=session_key).order_by("created_at").first()
    if cart is None:
        cart = Cart.objects.create(session_key=session_key)

    response = Response(
        {"success": True, "cart_id": str(cart.id), "cart": cart_payload(cart)},
        status=status.HTTP_200_OK,
    )
    response.set_cookie(
        "cart_id",
        str(cart.id),
        max_age=30 * 24 * 60 * 60,
        path="/",
        secure=request.is_secure(),
        httponly=False,
        samesite="Lax",
    )
    return response


@api_view(["GET"])
def get_cart(request):
    cart_id = request.query_params.get("cart_id")
    if not cart_id:
        return Response(
            {"success": False, "code": "MISSING_CART_ID", "error": "Missing cart_id"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        cart = _cart_by_id(cart_id)
    except (Cart.DoesNotExist, ValidationError, ValueError) as exc:
        return _cart_error(exc)

    return Response({"success": True, "cart": cart_payload(cart)})


@api_view(["POST"])
def add_to_cart(request):
    cart_id = request.data.get("cart_id")
    product_id = request.data.get("product_id")

    if not cart_id or not product_id:
        return Response(
            {
                "success": False,
                "code": "MISSING_FIELDS",
                "error": "Cart ID and product ID are required",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        quantity = _integer(
            request.data.get("quantity", 1),
            field_name="Quantity",
            minimum=1,
            maximum=MAX_CART_QUANTITY,
        )
    except ValueError as exc:
        return Response(
            {"success": False, "code": "INVALID_QUANTITY", "error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        product = Product.objects.get(id=product_id)
    except (Product.DoesNotExist, ValidationError, ValueError):
        return Response(
            {"success": False, "code": "PRODUCT_NOT_FOUND", "error": "Product not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        with transaction.atomic():
            cart = _cart_by_id(cart_id, lock=True)
            item = CartItem.objects.filter(cart=cart, product=product).first()
            created = item is None

            if item is None:
                item = CartItem(cart=cart, product=product, quantity=quantity)
            else:
                new_quantity = item.quantity + quantity
                if new_quantity > MAX_CART_QUANTITY:
                    return Response(
                        {
                            "success": False,
                            "code": "INVALID_QUANTITY",
                            "error": f"Quantity cannot be greater than {MAX_CART_QUANTITY}",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                item.quantity = new_quantity

            item.save()
            cart.save(update_fields=["updated_at"])
    except (Cart.DoesNotExist, ValidationError, ValueError) as exc:
        return _cart_error(exc)

    return Response(
        {
            "success": True,
            "created": created,
            "message": "Item added to cart",
            "cart": cart_payload(cart),
        },
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["POST", "PATCH"])
def update_cart_item(request):
    item_id = request.data.get("item_id")
    cart_id = request.data.get("cart_id")

    if not item_id or not cart_id:
        return Response(
            {
                "success": False,
                "code": "MISSING_FIELDS",
                "error": "Item ID and cart ID are required",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        quantity = _integer(
            request.data.get("quantity"),
            field_name="Quantity",
            minimum=0,
            maximum=MAX_CART_QUANTITY,
        )
    except ValueError as exc:
        return Response(
            {"success": False, "code": "INVALID_QUANTITY", "error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with transaction.atomic():
            cart = _cart_by_id(cart_id, lock=True)
            item = CartItem.objects.select_for_update().get(id=item_id, cart=cart)

            if quantity == 0:
                item.delete()
                message = "Item removed from cart"
            else:
                item.quantity = quantity
                item.save(update_fields=["quantity", "updated_at"])
                message = "Cart quantity updated"

            cart.save(update_fields=["updated_at"])
    except (Cart.DoesNotExist, ValidationError, ValueError) as exc:
        return _cart_error(exc)
    except CartItem.DoesNotExist:
        return Response(
            {"success": False, "code": "ITEM_NOT_FOUND", "error": "Cart item not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    return Response(
        {"success": True, "message": message, "cart": cart_payload(cart)}
    )


@api_view(["POST", "DELETE"])
def remove_from_cart(request):
    item_id = request.data.get("item_id")
    cart_id = request.data.get("cart_id")

    if not item_id or not cart_id:
        return Response(
            {
                "success": False,
                "code": "MISSING_FIELDS",
                "error": "Item ID and cart ID are required",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with transaction.atomic():
            cart = _cart_by_id(cart_id, lock=True)
            deleted, _ = CartItem.objects.filter(id=item_id, cart=cart).delete()
            if not deleted:
                return Response(
                    {
                        "success": False,
                        "code": "ITEM_NOT_FOUND",
                        "error": "Cart item not found",
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )
            cart.save(update_fields=["updated_at"])
    except (Cart.DoesNotExist, ValidationError, ValueError) as exc:
        return _cart_error(exc)

    return Response(
        {
            "success": True,
            "message": "Item removed from cart",
            "cart": cart_payload(cart),
        }
    )


@api_view(["POST", "DELETE"])
def clear_cart(request):
    cart_id = request.data.get("cart_id") or request.query_params.get("cart_id")
    if not cart_id:
        return Response(
            {"success": False, "code": "MISSING_CART_ID", "error": "Missing cart_id"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with transaction.atomic():
            cart = _cart_by_id(cart_id, lock=True)
            cart.items.all().delete()
            cart.save(update_fields=["updated_at"])
    except (Cart.DoesNotExist, ValidationError, ValueError) as exc:
        return _cart_error(exc)

    return Response(
        {"success": True, "message": "Cart cleared", "cart": cart_payload(cart)}
    )
