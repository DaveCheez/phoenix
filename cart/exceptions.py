from rest_framework import status


class CartOptionError(Exception):
    """Raised when selected cart options fail public validation."""

    def __init__(self, code, error, *, errors=None, http_status=None):
        super().__init__(error)
        self.code = code
        self.error = error
        self.errors = errors
        self.http_status = http_status or status.HTTP_400_BAD_REQUEST
