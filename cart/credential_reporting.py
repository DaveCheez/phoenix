"""Redact guest credentials from Django's production exception reports.

Django's default reporter filter hides settings whose names match
API, TOKEN, KEY, SECRET, PASS or SIGNATURE. ``HTTP_AUTHORIZATION`` and
``HTTP_COOKIE`` do not match those names. ``CREDENTIAL`` is not in that
list either, so ``CART_APP_CREDENTIAL`` and the application header need
an explicit match. The text report prints ``request.COOKIES`` without
passing them through that filter.
"""

import re

from django.views.debug import ExceptionReporter, SafeExceptionReporterFilter


class CartExceptionReporterFilter(SafeExceptionReporterFilter):
    hidden_settings = re.compile(
        "API|TOKEN|KEY|SECRET|PASS|SIGNATURE|CREDENTIAL|SHOPPER_ADDRESS",
        re.IGNORECASE,
    )
    redacted_meta = frozenset(
        {
            "HTTP_AUTHORIZATION",
            "HTTP_COOKIE",
            "HTTP_X_PHOENIX_APP_CREDENTIAL",
            "HTTP_X_PHOENIX_SHOPPER_ADDRESS",
        }
    )

    def get_safe_request_meta(self, request):
        cleaned = super().get_safe_request_meta(request)
        for key in self.redacted_meta:
            if key in cleaned:
                cleaned[key] = self.cleansed_substitute
        return cleaned


class CartExceptionReporter(ExceptionReporter):
    def get_traceback_data(self):
        data = super().get_traceback_data()
        cookies = data.get("request_COOKIES_items")
        if cookies:
            data["request_COOKIES_items"] = [
                (key, self.filter.cleansed_substitute) for key, _value in cookies
            ]
        return data
