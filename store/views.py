import logging

from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import ParseError, PermissionDenied, Throttled
from rest_framework.response import Response
from rest_framework.views import APIView

from .enquiry_email import send_enquiry_email
from .models import Category, Enquiry, HomeSlide, Product, ProductOption, ProductOptionGroup, Review
from .permissions import ContactProxyPermission
from .serializers import (
    CategorySerializer,
    CategoryWithProductsSerializer,
    ContactEnquirySerializer,
    HomeSlideSerializer,
    ProductListSerializer,
    ProductSerializer,
    ReviewSerializer,
)
from .throttles import ContactEnquiryThrottle


logger = logging.getLogger(__name__)


def categories(request):
    return {"categories": Category.objects.all()}


def get_products(request):
    products = Product.objects.all()
    return render(request, "store/index.html", {"products": products})


def category_list(request, category_slug=None):
    category = get_object_or_404(Category, slug=category_slug)
    products = Product.objects.filter(category=category)
    return render(
        request,
        "store/products/category.html",
        {"category": category, "products": products},
    )


def product_detail(request, slug):
    product = get_object_or_404(Product, slug=slug)
    images = product.productimage_set.all()
    return render(
        request,
        "store/products/single.html",
        {"product": product, "images": images},
    )


def about(request):
    return render(request, "store/about.html")


class ProductListAPI(generics.ListAPIView):
    serializer_class = ProductListSerializer

    def get_queryset(self):
        queryset = Product.objects.all().prefetch_related("productimage_set")
        category_id = self.request.query_params.get("category_id")
        if category_id:
            queryset = queryset.filter(category_id=category_id)
        return queryset


class ProductDetailAPI(generics.RetrieveAPIView):
    queryset = Product.objects.prefetch_related(
        "productimage_set",
        Prefetch(
            "option_groups",
            queryset=ProductOptionGroup.objects.filter(active=True)
            .order_by("display_order", "id")
            .prefetch_related(
                Prefetch(
                    "options",
                    queryset=ProductOption.objects.filter(
                        active=True,
                        group__isnull=False,
                    ).order_by("display_order", "id"),
                )
            ),
        ),
    )
    serializer_class = ProductSerializer
    lookup_field = "slug"


class CategoryListAPI(generics.ListAPIView):
    serializer_class = CategorySerializer

    def get_queryset(self):
        category_type = self.request.query_params.get("type", "product")
        return Category.objects.filter(type=category_type).prefetch_related(
            "categoryimage_set"
        )


class CategoryDetailAPI(generics.RetrieveAPIView):
    queryset = Category.objects.prefetch_related(
        "categoryimage_set",
        "store__productimage_set",
    )
    serializer_class = CategoryWithProductsSerializer
    lookup_field = "slug"


class ReviewListAPI(generics.ListAPIView):
    queryset = Review.objects.all()
    serializer_class = ReviewSerializer


class HomeSlideListAPI(generics.ListAPIView):
    serializer_class = HomeSlideSerializer

    def get_queryset(self):
        now = timezone.now()
        return (
            HomeSlide.objects.filter(is_active=True)
            .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=now))
            .order_by("display_order", "id")
        )


ENQUIRY_RECEIVED_MESSAGE = (
    "Thanks — your enquiry has been received. We will get back to you shortly."
)


def _email_failure_category(exc):
    max_length = Enquiry._meta.get_field("email_error").max_length
    return type(exc).__name__[:max_length]


def _persist_email_status(enquiry, *, email_status, email_sent_at, email_error):
    try:
        updated = Enquiry.objects.filter(pk=enquiry.pk).update(
            email_status=email_status,
            email_sent_at=email_sent_at,
            email_error=email_error,
        )
        if updated != 1:
            raise RuntimeError("Enquiry delivery status row was not updated")
    except Exception:
        logger.exception(
            "contact_enquiry_status_update_failed",
            extra={
                "enquiry_reference": enquiry.reference,
                "target_email_status": email_status,
            },
        )
        return False

    enquiry.email_status = email_status
    enquiry.email_sent_at = email_sent_at
    enquiry.email_error = email_error
    return True


class ContactEnquiryAPI(APIView):
    authentication_classes = []
    permission_classes = [ContactProxyPermission]
    throttle_classes = [ContactEnquiryThrottle]

    def handle_exception(self, exc):
        if isinstance(exc, PermissionDenied):
            return Response(
                {
                    "success": False,
                    "code": "CONTACT_PROXY_FORBIDDEN",
                    "error": "Contact request is not authorised.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if isinstance(exc, Throttled):
            return Response(
                {
                    "success": False,
                    "code": "RATE_LIMITED",
                    "error": (
                        "Too many enquiries have been submitted. "
                        "Please try again later."
                    ),
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if isinstance(exc, ParseError):
            return Response(
                {
                    "success": False,
                    "code": "VALIDATION_ERROR",
                    "error": "Please correct the highlighted fields.",
                    "errors": {
                        "non_field_errors": [
                            "The request body is not valid JSON."
                        ]
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().handle_exception(exc)

    def post(self, request):
        serializer = ContactEnquirySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "success": False,
                    "code": "VALIDATION_ERROR",
                    "error": "Please correct the highlighted fields.",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        enquiry = serializer.save()
        response_code = "ENQUIRY_RECEIVED"

        try:
            send_enquiry_email(enquiry)
        except Exception as exc:
            logger.exception(
                "contact_enquiry_email_delivery_failed",
                extra={"enquiry_reference": enquiry.reference},
            )
            _persist_email_status(
                enquiry,
                email_status=Enquiry.EmailStatus.FAILED,
                email_sent_at=None,
                email_error=_email_failure_category(exc),
            )
            response_code = "ENQUIRY_SAVED_EMAIL_FAILED"
        else:
            _persist_email_status(
                enquiry,
                email_status=Enquiry.EmailStatus.SENT,
                email_sent_at=timezone.now(),
                email_error="",
            )

        return Response(
            {
                "success": True,
                "code": response_code,
                "message": ENQUIRY_RECEIVED_MESSAGE,
                "reference": enquiry.reference,
            },
            status=status.HTTP_201_CREATED,
        )
