class OrderCreationError(Exception):
    """Raised when an internal order snapshot cannot be created.

    ``code`` is stable for callers. ``message`` must stay safe to show later:
    no database text and no customer-supplied values.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
