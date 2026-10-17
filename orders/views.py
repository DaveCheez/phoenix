"""Authenticated public order creation and confirmation.

Application authentication runs first. The guest bearer is the only customer
credential. Prices in the body are rejected rather than stored.
"""

from django.views.decorators.debug import sensitive_variables
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from cart.application_access import ApplicationAccessRejected, authenticate_cart_application
from cart.exceptions import CartOptionError
from cart.guest_access import GuestAccessError, get_guest_session
from cart.http_access import GuestCartMissing, read_bearer

from .access import (
    IdempotencyConflict,
    order_for_guest,
    parse_idempotency_key,
    public_order,
    submit_guest_order,
)
from .exceptions import OrderCreationError


_CUSTOMER_FIELDS = {
    "customer_name",
    "customer_email",
    "customer_phone",
    "vehicle_make",
    "vehicle_model",
    "vehicle_year",
    "vehicle_wheelbase",
    "vehicle_registration",
    "customer_notes",
}
_OPTIONAL = {"vehicle_registration", "customer_notes"}

_APPLICATION_REJECTED = {
    "success": False,
    "code": "CART_APPLICATION_REJECTED",
    "error": "Cart service is temporarily unavailable.",
}
_ACCESS_DENIED = {
    "success": False,
    "code": "CART_ACCESS_UNAVAILABLE",
    "error": "Cart access is not available.",
}
_INVALID = {
    "success": False,
    "code": "ORDER_REQUEST_INVALID",
    "error": "The order request is not valid.",
}
_UNAVAILABLE = {
    "success": False,
    "code": "ORDER_NOT_AVAILABLE",
    "error": "This order is not available.",
}


def _finish(response, *, authenticate=False):
    response["Cache-Control"] = "no-store"
    if authenticate:
        response["WWW-Authenticate"] = 'Bearer realm="cart"'
    return response


def _application_rejected():
    return _finish(Response(_APPLICATION_REJECTED, status=status.HTTP_503_SERVICE_UNAVAILABLE))


def _access_denied():
    return _finish(Response(_ACCESS_DENIED, status=status.HTTP_401_UNAUTHORIZED), authenticate=True)


def _invalid():
    return _finish(Response(_INVALID, status=status.HTTP_400_BAD_REQUEST))


def _unavailable():
    return _finish(Response(_UNAVAILABLE, status=status.HTTP_404_NOT_FOUND))


@sensitive_variables()
def _gate(request):
    try:
        authenticate_cart_application(request)
    except ApplicationAccessRejected:
        return _application_rejected()
    return None


def _customer_body(data):
    if not isinstance(data, dict) or set(data) - _CUSTOMER_FIELDS:
        raise OrderCreationError("ORDER_REQUEST_INVALID", "The order request is not valid.")
    if not _CUSTOMER_FIELDS - _OPTIONAL <= set(data):
        raise OrderCreationError("ORDER_REQUEST_INVALID", "The order request is not valid.")
    values = {name: data.get(name, "") for name in _CUSTOMER_FIELDS}
    year = values["vehicle_year"]
    if isinstance(year, bool) or not isinstance(year, int):
        raise OrderCreationError("INVALID_VEHICLE", "The vehicle details are not valid.")
    return values


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([JSONParser])
@sensitive_variables()
def create_order(request):
    rejected = _gate(request)
    if rejected is not None:
        return rejected
    try:
        bearer = read_bearer(request)
    except GuestAccessError:
        return _access_denied()
    if bearer.absent:
        return _access_denied()
    try:
        key = parse_idempotency_key(request.headers.get("Idempotency-Key"))
        fields = _customer_body(request.data)
        order, replayed = submit_guest_order(
            raw_token=bearer.token,
            idempotency_key=key,
            customer_fields=fields,
        )
    except (OrderCreationError, CartOptionError, GuestAccessError, GuestCartMissing) as exc:
        return _creation_error(exc)
    code = status.HTTP_200_OK if replayed else status.HTTP_201_CREATED
    return _finish(Response({"success": True, "order": public_order(order)}, status=code))


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@sensitive_variables()
def order_confirmation(request, reference):
    rejected = _gate(request)
    if rejected is not None:
        return rejected
    try:
        bearer = read_bearer(request)
    except GuestAccessError:
        return _access_denied()
    if bearer.absent:
        return _access_denied()
    try:
        session = get_guest_session(raw_token=bearer.token, for_update=False)
    except GuestAccessError:
        return _access_denied()
    order = order_for_guest(session=session, reference=reference)
    if order is None:
        return _unavailable()
    return _finish(Response({"success": True, "order": public_order(order)}))


def _creation_error(exc):
    if isinstance(exc, (GuestAccessError, GuestCartMissing)):
        return _access_denied()
    if isinstance(exc, CartOptionError):
        return _finish(
            Response(
                {"success": False, "code": exc.code, "error": exc.error},
                status=exc.http_status,
            )
        )
    if isinstance(exc, IdempotencyConflict):
        return _finish(
            Response(
                {"success": False, "code": exc.code, "error": exc.message},
                status=status.HTTP_409_CONFLICT,
            )
        )
    status_code = status.HTTP_400_BAD_REQUEST
    if exc.code == "ORDER_TEMPORARILY_UNAVAILABLE":
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return _finish(
        Response(
            {"success": False, "code": exc.code, "error": exc.message},
            status=status_code,
        )
    )
