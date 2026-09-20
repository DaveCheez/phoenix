from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from django.views.generic import RedirectView


def health_check(_request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("health/", health_check, name="health-check"),
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
    path("nested_admin/", include("nested_admin.urls")),
    path("admin/", admin.site.urls),
    path("api/", include("store.api_urls")),
    path("api/cart/", include("cart.urls")),
    path("accounts/", include("accounts.urls", namespace="accounts")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
