"""Shared cart HTTP test client.

The credential is a fixed test value. It is not a production secret.
"""

from django.test import override_settings
from rest_framework.test import APIClient


TEST_APP_CREDENTIAL = "0123456789abcdef" * 4
TEST_SHOPPER_ADDRESS = "203.0.113.10"

def cart_app_settings(test):
    """Return a fresh settings override so classes do not share one decorator."""
    return override_settings(CART_APP_CREDENTIAL=TEST_APP_CREDENTIAL)(test)


class CartAPIClient(APIClient):
    """Send the application headers unless a test sets them itself."""

    def request(self, **kwargs):
        kwargs.setdefault("HTTP_X_PHOENIX_APP_CREDENTIAL", TEST_APP_CREDENTIAL)
        kwargs.setdefault("HTTP_X_PHOENIX_SHOPPER_ADDRESS", TEST_SHOPPER_ADDRESS)
        return super().request(**kwargs)
