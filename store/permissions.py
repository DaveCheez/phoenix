import ipaddress

from django.conf import settings
from django.utils.crypto import constant_time_compare, salted_hmac
from rest_framework.permissions import BasePermission


class ContactProxyPermission(BasePermission):
    message = "Contact request is not authorised."

    def has_permission(self, request, view):
        supplied_secret = request.headers.get("X-Phoenix-Contact-Secret", "")
        expected_secret = settings.CONTACT_PROXY_SECRET
        secret_is_valid = bool(expected_secret) and constant_time_compare(
            supplied_secret,
            expected_secret,
        )

        supplied_ip = request.headers.get("X-Phoenix-Client-IP", "")
        try:
            canonical_ip = ipaddress.ip_address(supplied_ip.strip()).compressed
        except ValueError:
            canonical_ip = None

        if not secret_is_valid or canonical_ip is None:
            return False

        request.contact_client_ip = canonical_ip
        request.contact_identity_hash = salted_hmac(
            "store.contact_rate_limit",
            canonical_ip,
            algorithm="sha256",
        ).hexdigest()
        return True
