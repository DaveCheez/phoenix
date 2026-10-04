class CartNoStoreMiddleware:
    """Mark every cart response uncacheable, including framework 405 and OPTIONS."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/api/cart/"):
            response["Cache-Control"] = "no-store"
        return response
