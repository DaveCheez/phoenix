import uuid

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from store.models import Product, ProductOption, ProductOptionGroup


class GuestSession(models.Model):
    """Anonymous checkout credential. Not a customer account or verified email.

    Only the SHA-256 digest of the raw token is stored. The raw token is
    issued once and is never written here. Expiry and revocation stay on this
    row so they remain available after the current cart is deleted.

    One guest session has at most one current cart. Later orders may still
    belong to the same guest session, and one source cart may still produce
    more than one order. Those order links are not part of this model.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(editable=False)
    expires_at = models.DateTimeField(editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ("-created_at", "-id")

    def __str__(self):
        return "Guest session"


class Cart(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="carts",
    )
    session_key = models.CharField(max_length=40, null=True, blank=True)
    guest_session = models.OneToOneField(
        GuestSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="cart",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-created_at")

    def __str__(self):
        return f"Cart ({self.user or self.session_key or self.id})"


class CartItem(models.Model):
    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    configuration_signature = models.CharField(
        max_length=512,
        default="",
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("cart", "product", "configuration_signature"),
                name="unique_product_configuration_per_cart",
            ),
            models.CheckConstraint(
                check=Q(quantity__gte=1),
                name="cart_item_quantity_gte_1",
            ),
        ]

    def __str__(self):
        return f"{self.product} x {self.quantity}"


class CartItemOption(models.Model):
    cart_item = models.ForeignKey(
        CartItem,
        on_delete=models.CASCADE,
        related_name="selected_options",
    )
    group = models.ForeignKey(
        ProductOptionGroup,
        on_delete=models.PROTECT,
        related_name="cart_item_selections",
    )
    option = models.ForeignKey(
        ProductOption,
        on_delete=models.PROTECT,
        related_name="cart_item_selections",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("group_id", "option_id", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("cart_item", "group"),
                name="unique_group_per_cart_item",
            ),
            models.UniqueConstraint(
                fields=("cart_item", "option"),
                name="unique_option_per_cart_item",
            ),
        ]

    def clean(self):
        super().clean()
        option = self._related_or_none("option")
        group = self._related_or_none("group")
        cart_item = self._related_or_none("cart_item")

        if option is not None and option.group_id is None:
            raise ValidationError(
                {"option": "Selected option must belong to an option group."}
            )

        if option is not None and group is not None and option.group_id != group.id:
            raise ValidationError(
                {"option": "Selected option does not belong to this group."}
            )

        if group is not None and cart_item is not None:
            if group.product_id != cart_item.product_id:
                raise ValidationError(
                    {"group": "Option group does not belong to this product."}
                )

    def _related_or_none(self, field_name):
        try:
            return getattr(self, field_name)
        except ObjectDoesNotExist:
            return None

    def __str__(self):
        return f"{self.cart_item} — {self.option}"


RATE_LIMIT_SCOPES = ("issuance", "failed_access", "authenticated_cart")
RATE_LIMIT_MAX_WINDOW_SECONDS = 86400


class CartRateLimitCounter(models.Model):
    """Fixed-window attempt counter. Not a customer quota and not a cart.

    ``subject_key`` is an HMAC digest. This row does not store an address,
    credential, customer, or request body, and it does not reference a cart,
    guest session, or order. ``expires_at`` is storage grace only; nothing
    deletes the row until a cleanup helper is scheduled.
    """

    scope = models.CharField(max_length=32)
    subject_key = models.CharField(max_length=64)
    window_seconds = models.PositiveIntegerField()
    window_start = models.DateTimeField()
    count = models.BigIntegerField()
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("scope", "subject_key", "window_seconds", "window_start"),
                name="cart_rate_limit_window_uniq",
            ),
            models.CheckConstraint(
                check=Q(scope__in=RATE_LIMIT_SCOPES),
                name="cart_rate_limit_counter_scope_valid",
            ),
            models.CheckConstraint(
                check=Q(count__gte=0),
                name="cart_rate_limit_counter_count_gte_0",
            ),
            models.CheckConstraint(
                check=Q(window_seconds__gte=1)
                & Q(window_seconds__lte=RATE_LIMIT_MAX_WINDOW_SECONDS),
                name="cart_rate_limit_counter_window_range",
            ),
            models.CheckConstraint(
                check=Q(expires_at__gt=models.F("window_start")),
                name="cart_rate_limit_counter_expiry_after_start",
            ),
        ]

    def __str__(self):
        return f"Cart rate limit {self.scope} {self.window_seconds}s"
