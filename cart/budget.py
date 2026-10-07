"""Application-only budget check for later Nuxt CSRF and reset handlers.

This route does not look up a guest session, cart, or bearer. A successful
response lets the calling server continue its own guarded operation. It is
not a reusable permit and it does not prove which customer owns a basket.

Both operations fail closed when the counter store or the enabled
configuration is unusable. That is separate from authenticated cart
accounting, which still proceeds when only its counter store is down.
"""

import logging

from django.conf import settings
from django.views.decorators.debug import sensitive_variables
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .application_access import ApplicationAccessRejected, authenticate_cart_application
from .rate_limit import (
    RateLimitNestedTransactionError,
    RateLimitStoreError,
    RateLimitValidationError,
    consume_windows,
)
from .rate_limit_http import (
    FRONTEND_POLICY_SCOPES,
    RateLimitConfigurationError,
    enforcement_enabled,
    policy_windows,
)
from .rate_limit_subjects import SubjectKeyError, subject_key_for_address


logger = logging.getLogger(__name__)

_OPERATION_SCOPES = {"csrf": "csrf", "reset": "reset"}
_ALLOWED_BODY = {"success": True, "code": "CART_BUDGET_ALLOWED"}
_INVALID_BODY = {
    "success": False,
    "code": "CART_BUDGET_REQUEST_INVALID",
    "error": "Cart budget request is not valid.",
}
_UNAVAILABLE_BODY = {
    "success": False,
    "code": "CART_TEMPORARILY_UNAVAILABLE",
    "error": "Cart access could not be completed.",
}
_LIMITED_BODY = {
    "success": False,
    "code": "CART_RATE_LIMITED",
    "error": "Cart access is temporarily limited.",
}
_APPLICATION_REJECTED_BODY = {
    "success": False,
    "code": "CART_APPLICATION_REJECTED",
    "error": "Cart service is temporarily unavailable.",
}


class _InvalidBudgetRequest(Exception):
    """The body is not one fixed operation. Submitted values stay out of the message."""


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([JSONParser])
@sensitive_variables()
def post_budget(request):
    """Count one CSRF or reset attempt for the accepted shopper address."""
    try:
        address = authenticate_cart_application(request)
    except ApplicationAccessRejected:
        logger.warning("cart_application_rejected")
        return _finish(
            Response(_APPLICATION_REJECTED_BODY, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        )
    try:
        operation = _operation(request)
    except (_InvalidBudgetRequest, ParseError):
        return _invalid()
    return account_budget(address, operation)


@sensitive_variables()
def account_budget(address, operation):
    """Commit one frontend-scope attempt, or allow immediately when disabled."""
    scope = _OPERATION_SCOPES[operation]
    try:
        if not enforcement_enabled():
            return _allowed()
        windows = policy_windows(
            raw=getattr(settings, "CART_FRONTEND_RATE_LIMIT_POLICIES", ""),
            required_scopes=FRONTEND_POLICY_SCOPES,
            scope=scope,
        )
        subject_key = subject_key_for_address(scope=scope, address=address)
        decision = consume_windows(
            scope=scope,
            subject_key=subject_key,
            windows=windows,
        )
    except RateLimitStoreError:
        logger.warning("cart_rate_limit_store_unavailable %s", scope)
        return _unavailable()
    except (
        RateLimitConfigurationError,
        RateLimitValidationError,
        RateLimitNestedTransactionError,
        SubjectKeyError,
    ):
        logger.warning("cart_rate_limit_configuration_rejected")
        return _unavailable()
    if decision.allowed:
        return _allowed()
    return _limited(decision.retry_after_seconds)


@sensitive_variables()
def _operation(request):
    try:
        data = request.data
    except ParseError:
        raise _InvalidBudgetRequest() from None
    if not isinstance(data, dict):
        raise _InvalidBudgetRequest()
    if set(data) != {"operation"}:
        raise _InvalidBudgetRequest()
    operation = data.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATION_SCOPES:
        raise _InvalidBudgetRequest()
    return operation


def _allowed():
    return _finish(Response(_ALLOWED_BODY, status=status.HTTP_200_OK))


def _invalid():
    return _finish(Response(_INVALID_BODY, status=status.HTTP_400_BAD_REQUEST))


def _limited(retry_after):
    response = _finish(
        Response(_LIMITED_BODY, status=status.HTTP_429_TOO_MANY_REQUESTS)
    )
    response["Retry-After"] = str(int(retry_after))
    return response


def _unavailable():
    return _finish(
        Response(_UNAVAILABLE_BODY, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    )


def _finish(response):
    response["Cache-Control"] = "no-store"
    return response
