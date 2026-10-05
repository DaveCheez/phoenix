import logging

from django.core.exceptions import ValidationError
from django.db import InterfaceError, OperationalError, transaction
from django.views.decorators.debug import sensitive_variables
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from store.models import Product

from .exceptions import CartOptionError
from .guest_access import GuestAccessError, issue_guest_cart, replace_missing_guest_cart
from .http_access import (
    GuestCartMissing,
    consistent_cart_id,
    is_exact_start_body,
    read_bearer,
    resolve_guest_cart,
    single_action,
    supplied_cart_ids,
)
from .models import CartItem
from .operations import MAX_CART_QUANTITY, add_product_to_cart
from .option_selection import reject_browser_prices
from .services import cart_payload


logger = logging.getLogger(__name__)

_ACCESS_BODY = {
    "success": False,
    "code": "CART_ACCESS_UNAVAILABLE",
    "error": "Cart access is not available.",
}
_MISSING_CART_BODY = {
    "success": False,
    "code": "GUEST_CART_MISSING",
    "error": "This guest session has no cart.",
}
_UNAVAILABLE_BODY = {
    "success": False,
    "code": "CART_TEMPORARILY_UNAVAILABLE",
    "error": "Cart access could not be completed.",
}
_START_REQUIRED_BODY = {
    "success": False,
    "code": "START_REQUIRED",
    "error": "An explicit start is required to create a guest cart.",
}


class _QuantityRejected(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class _ItemMissing(Exception):
    pass


class _ProductMissing(Exception):
    pass


def _integer(value, *, field_name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise _QuantityRejected(f"{field_name} must be a whole number") from None

    if parsed < minimum:
        raise _QuantityRejected(f"{field_name} must be at least {minimum}")
    if parsed > maximum:
        raise _QuantityRejected(f"{field_name} cannot be greater than {maximum}")
    return parsed


def _finish(response, *, authenticate=False):
    response["Cache-Control"] = "no-store"
    if authenticate:
        response["WWW-Authenticate"] = 'Bearer realm="cart"'
    return response


def _access_denied():
    return _finish(Response(_ACCESS_BODY, status=status.HTTP_401_UNAUTHORIZED), authenticate=True)


def _missing_cart():
    return _finish(Response(_MISSING_CART_BODY, status=status.HTTP_409_CONFLICT))


def _unavailable():
    logger.warning("cart_database_unavailable")
    return _finish(
        Response(_UNAVAILABLE_BODY, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    )


def _start_required():
    return _finish(Response(_START_REQUIRED_BODY, status=status.HTTP_400_BAD_REQUEST))


def _option_error(exc: CartOptionError) -> Response:
    payload = {"success": False, "code": exc.code, "error": exc.error}
    if exc.errors:
        payload["errors"] = exc.errors
    return _finish(Response(payload, status=exc.http_status))


def _quantity_error(exc: _QuantityRejected) -> Response:
    return _finish(
        Response(
            {"success": False, "code": "INVALID_QUANTITY", "error": exc.message},
            status=status.HTTP_400_BAD_REQUEST,
        )
    )


def _item_missing():
    return _finish(
        Response(
            {"success": False, "code": "ITEM_NOT_FOUND", "error": "Cart item not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    )


def _product_missing():
    return _finish(
        Response(
            {"success": False, "code": "PRODUCT_NOT_FOUND", "error": "Product not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    )


def _missing_fields(error):
    return _finish(
        Response(
            {"success": False, "code": "MISSING_FIELDS", "error": error},
            status=status.HTTP_400_BAD_REQUEST,
        )
    )


def _cart_response(cart, *, status_code, extra=None):
    payload = {"success": True, "cart": cart_payload(cart)}
    if extra:
        payload.update(extra)
    return _finish(Response(payload, status=status_code))


def _response_for(exc):
    if isinstance(exc, GuestAccessError):
        return _access_denied()
    if isinstance(exc, GuestCartMissing):
        return _missing_cart()
    if isinstance(exc, CartOptionError):
        return _option_error(exc)
    if isinstance(exc, _QuantityRejected):
        return _quantity_error(exc)
    if isinstance(exc, _ItemMissing):
        return _item_missing()
    if isinstance(exc, _ProductMissing):
        return _product_missing()
    if isinstance(exc, (OperationalError, InterfaceError)):
        return _unavailable()
    return None


@sensitive_variables()
def _require_token(request):
    bearer = read_bearer(request)
    if bearer.absent or not bearer.token:
        raise GuestAccessError()
    return bearer.token


@sensitive_variables()
def _read_cart(request, raw_token):
    return resolve_guest_cart(
        raw_token=raw_token,
        asserted_cart_id=consistent_cart_id(request),
        for_update=False,
    )


@sensitive_variables()
def _mutate(request, perform):
    try:
        raw_token = _require_token(request)
        with transaction.atomic():
            cart = resolve_guest_cart(
                raw_token=raw_token,
                asserted_cart_id=consistent_cart_id(request),
                for_update=True,
            )
            return perform(request, cart)
    except Exception as exc:
        response = _response_for(exc)
        if response is None:
            raise
        return response


def _load_product(product_id):
    try:
        return Product.objects.get(id=product_id)
    except (Product.DoesNotExist, ValidationError, ValueError, TypeError):
        raise _ProductMissing() from None


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@sensitive_variables()
def create_cart(request):
    """Start a guest cart, or return the cart for a bearer already presented."""
    try:
        bearer = read_bearer(request)
    except GuestAccessError:
        return _access_denied()
    except (OperationalError, InterfaceError):
        return _unavailable()

    if bearer.absent:
        return _start_without_credential(request)
    return _open_authenticated_cart(request, bearer.token)


@sensitive_variables()
def _render_negotiated_response(request, response):
    """Serialize this response with the renderer already chosen for it.

    Constructing a DRF ``Response`` does not render its body. Django does
    that after the view returns. Issuance renders this same object first,
    using the renderer and media type ``APIView.initial`` stored on the
    request, so preparation stays inside the issuance transaction.
    """
    renderer = getattr(request, "accepted_renderer", None)
    media_type = getattr(request, "accepted_media_type", None)
    if renderer is None or not media_type:
        raise RuntimeError("Cart issuance cannot render before content negotiation.")
    parser_context = getattr(request, "parser_context", None) or {}
    response.accepted_renderer = renderer
    response.accepted_media_type = media_type
    response.renderer_context = {
        "view": parser_context.get("view"),
        "args": parser_context.get("args", ()),
        "kwargs": parser_context.get("kwargs", {}),
        "request": request,
    }
    return response.render()


@sensitive_variables()
def _start_without_credential(request):
    if supplied_cart_ids(request) or single_action(request) == "replace_missing":
        return _access_denied()
    if not is_exact_start_body(request):
        return _start_required()
    try:
        with transaction.atomic():
            issued = issue_guest_cart()
            session = issued.cart.guest_session
            response = _finish(
                Response(
                    {
                        "success": True,
                        "guest_access": {
                            "token": issued.raw_token,
                            "expires_at": session.expires_at.isoformat(),
                        },
                        "cart": cart_payload(issued.cart),
                    },
                    status=status.HTTP_201_CREATED,
                )
            )
            return _render_negotiated_response(request, response)
    except (OperationalError, InterfaceError):
        return _unavailable()


@sensitive_variables()
def _open_authenticated_cart(request, raw_token):
    if single_action(request) == "replace_missing":
        if supplied_cart_ids(request):
            return _access_denied()
        try:
            cart, created = replace_missing_guest_cart(raw_token=raw_token)
        except Exception as exc:
            response = _response_for(exc)
            if response is None:
                raise
            return response
        return _cart_response(
            cart,
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    try:
        cart = _read_cart(request, raw_token)
        return _cart_response(cart, status_code=status.HTTP_200_OK)
    except Exception as exc:
        response = _response_for(exc)
        if response is None:
            raise
        return response


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@sensitive_variables()
def get_cart(request):
    try:
        cart = _read_cart(request, _require_token(request))
        return _cart_response(cart, status_code=status.HTTP_200_OK)
    except Exception as exc:
        response = _response_for(exc)
        if response is None:
            raise
        return response


def _add(request, cart):
    if not request.data.get("product_id"):
        return _missing_fields("Product ID is required")
    reject_browser_prices(request.data)
    quantity = _integer(
        request.data.get("quantity", 1),
        field_name="Quantity",
        minimum=1,
        maximum=MAX_CART_QUANTITY,
    )
    product = _load_product(request.data.get("product_id"))
    _item, created = add_product_to_cart(
        cart=cart,
        product=product,
        quantity=quantity,
        raw_options=request.data.get("options"),
    )
    return _cart_response(
        cart,
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        extra={"created": created, "message": "Item added to cart"},
    )


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@sensitive_variables()
def add_to_cart(request):
    """Add a line. A lost response can follow a successful commit.

    The write is not idempotent. A client that does not see this response
    must not assume that repeating the add is safe.
    """
    return _mutate(request, _add)


def _update(request, cart):
    item_id = request.data.get("item_id")
    if not item_id:
        return _missing_fields("Item ID is required")
    quantity = _integer(
        request.data.get("quantity"),
        field_name="Quantity",
        minimum=0,
        maximum=MAX_CART_QUANTITY,
    )
    try:
        item = CartItem.objects.select_for_update().get(id=item_id, cart=cart)
    except (CartItem.DoesNotExist, ValidationError, ValueError, TypeError):
        raise _ItemMissing() from None

    if quantity == 0:
        item.delete()
        message = "Item removed from cart"
    else:
        item.quantity = quantity
        item.save(update_fields=["quantity", "updated_at"])
        message = "Cart quantity updated"
    cart.save(update_fields=["updated_at"])
    return _cart_response(
        cart,
        status_code=status.HTTP_200_OK,
        extra={"message": message},
    )


@api_view(["POST", "PATCH"])
@authentication_classes([])
@permission_classes([AllowAny])
@sensitive_variables()
def update_cart_item(request):
    return _mutate(request, _update)


def _remove(request, cart):
    item_id = request.data.get("item_id")
    if not item_id:
        return _missing_fields("Item ID is required")
    try:
        deleted, _rows = CartItem.objects.filter(id=item_id, cart=cart).delete()
    except (ValidationError, ValueError, TypeError):
        raise _ItemMissing() from None
    if not deleted:
        raise _ItemMissing()
    cart.save(update_fields=["updated_at"])
    return _cart_response(
        cart,
        status_code=status.HTTP_200_OK,
        extra={"message": "Item removed from cart"},
    )


@api_view(["POST", "DELETE"])
@authentication_classes([])
@permission_classes([AllowAny])
@sensitive_variables()
def remove_from_cart(request):
    return _mutate(request, _remove)


def _clear(request, cart):
    cart.items.all().delete()
    cart.save(update_fields=["updated_at"])
    return _cart_response(
        cart,
        status_code=status.HTTP_200_OK,
        extra={"message": "Cart cleared"},
    )


@api_view(["POST", "DELETE"])
@authentication_classes([])
@permission_classes([AllowAny])
@sensitive_variables()
def clear_cart(request):
    return _mutate(request, _clear)
