import secrets
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import IntegrityError, models, transaction
from django.urls import reverse
from django.utils import timezone

from .image_processing import optimise_uploaded_image



class Category(models.Model):
    name = models.CharField(max_length=200, blank=False, null=False)
    slug = models.SlugField(max_length=50, unique=True)
    type = models.CharField(max_length=100, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    # image = models.ImageField(upload_to='static\images', default='images/default.png')
    
    class Meta:
        verbose_name_plural = 'Categories'

    def __str__(self):
        return self.name
    
    # REPLACE WITH REVERSE 
    def get_absolute_url(self):
        return reverse('store:category_list', args=[self.slug])



class Product(models.Model):
    category = models.ForeignKey(Category, null=True, related_name='store', on_delete=models.CASCADE)
    name = models.CharField(max_length=200, blank=False, null=False)
    slug = models.SlugField(max_length=50)
    description = models.TextField(blank=True, null=True)
    # image_main = models.ImageField(upload_to='static\images', default='images/default.png')
    price = models.DecimalField(max_digits=6, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'Products'
        ordering = ('-created_at',)

    def __str__(self):
        return self.name

    # REPLACE WITH REVERSE 
    def get_absolute_url(self):
        return f'/shop/{self.slug}'

    def get_featured_image(self):
        return self.productimage_set.filter(image_type='thumbnail').first()

    def get_featured_image_url(self):
        image = self.get_featured_image() or self.productimage_set.first()
        if not image or not image.image:
            return ""

        image_url = image.image.url
        if image_url.startswith(("http://", "https://")):
            return image_url
        return f"{settings.SITE_URL}/{image_url.lstrip('/')}"

class ProductImage(models.Model):
    IMAGE_TYPE_CHOICES = [
        ('', 'Unspecified'),
        ('thumbnail', 'Thumbnail'),
        ('gallery', 'Gallery'),
    ]

    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    name = models.CharField(max_length=120, blank=False, null=False)
    image = models.ImageField(upload_to='product_images/', default='product_images/default.png')
    slug = models.SlugField(max_length=50)
    image_type = models.CharField(
        max_length=10,
        choices=IMAGE_TYPE_CHOICES,
        blank=True,
        null=True,
        default=''
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.image_type or 'Unspecified'})"

class ProductOptionGroup(models.Model):
    product = models.ForeignKey(Product, related_name='option_groups', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)  # e.g. Wheelbase, Lights
    required = models.BooleanField(default=False)
    display_order = models.PositiveIntegerField(default=0)
    help_text = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("display_order", "id")

    def __str__(self):
        return f"{self.product.name} - {self.name} ({'Required' if self.required else 'Optional'})"


class ProductOption(models.Model):
    group = models.ForeignKey(
        ProductOptionGroup,
        related_name="options",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=100)  # e.g. MWB, LWB, 2x Lights
    price_adjustment = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    sku = models.CharField(max_length=50, blank=True, null=True)
    display_order = models.PositiveIntegerField(default=0)
    is_default = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("display_order", "id")
        constraints = [
            models.CheckConstraint(
                check=models.Q(price_adjustment__gte=Decimal("0.00")),
                name="productoption_price_adjustment_gte_0",
            ),
            models.UniqueConstraint(
                fields=("group",),
                condition=models.Q(is_default=True) & models.Q(group__isnull=False),
                name="unique_default_option_per_group",
            ),
        ]

    def clean(self):
        super().clean()
        if self.price_adjustment is not None and self.price_adjustment < Decimal("0.00"):
            raise ValidationError(
                {"price_adjustment": "Price adjustments cannot be negative."}
            )
        if self.is_default and self.group_id:
            qs = ProductOption.objects.filter(group_id=self.group_id, is_default=True)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError(
                    {"is_default": "Only one default option is allowed per group."}
                )

    def __str__(self):
        group_name = self.group.name if self.group_id else "Unassigned"
        return f"{group_name} - {self.name} (+£{self.price_adjustment})"


class CategoryImage(models.Model):
    IMAGE_TYPE_CHOICES = [
        ('', 'Unspecified'),   # <--- this adds the "not tagged" option
        ('header', 'Header'),
        ('thumbnail', 'Thumbnail'),
    ]

    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    image = models.ImageField(upload_to='category_images/', default='category_images/default.png')
    slug = models.SlugField(max_length=50)
    image_type = models.CharField(
        max_length=10,
        choices=IMAGE_TYPE_CHOICES,
        blank=True,
        null=True,
        default=''
    )

    def _optimisation_settings(self):
        """
        Return image limits appropriate to the category image's purpose.

        Header images retain more resolution for large category-page displays.
        Thumbnail and unspecified images use smaller limits because they are
        normally displayed as cards or navigation images.
        """
        if self.image_type == "header":
            return {
                "max_width": 1920,
                "max_height": 1200,
                "target_bytes": 500_000,
                "quality": 82,
                "minimum_quality": 62,
                "filename_prefix": "category-header",
            }

        return {
            "max_width": 1200,
            "max_height": 900,
            "target_bytes": 250_000,
            "quality": 80,
            "minimum_quality": 60,
            "filename_prefix": "category-thumbnail",
        }

    def save(self, *args, **kwargs):
        processed_image = False

        if self.image and not getattr(self.image, "_committed", True):
            settings = self._optimisation_settings()

            result = optimise_uploaded_image(
                self.image.file,
                **settings,
            )

            self.image = result.content
            processed_image = True

        # Ensure an explicitly supplied update_fields argument does not prevent
        # a newly assigned and processed image from being saved.
        if processed_image and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = (
                set(kwargs["update_fields"]) | {"image"}
            )

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.image_type or 'Unspecified'})"

class Review(models.Model):
    name = models.CharField(max_length=100)
    content = models.TextField()
    stars = models.PositiveSmallIntegerField(default=5)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.stars}★)"

class HomeSlide(models.Model):
    """Homepage hero content managed from Django admin."""

    title = models.CharField(max_length=160, default="Phoenix Vanz")
    subtitle = models.CharField(max_length=255, blank=True)
    image = models.ImageField(upload_to="home_slides/")
    mobile_image = models.ImageField(
        upload_to="home_slides/mobile/",
        blank=True,
        null=True,
        help_text="Optional portrait or mobile-specific image.",
    )
    button_text = models.CharField(max_length=80, blank=True)
    button_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="Use a relative path such as /shop/roof-racks or a full URL.",
    )
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(blank=True, null=True)
    ends_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def _optimise_new_upload(
        self,
        field_name,
        *,
        max_width,
        max_height,
        target_bytes,
        quality,
        minimum_quality,
        filename_prefix,
    ):
        """
        Optimise a newly assigned image.

        Existing committed files are left untouched, so changing the title,
        display order or publishing dates does not repeatedly recompress them.
        """
        field_file = getattr(self, field_name)

        if not field_file:
            return False

        if getattr(field_file, "_committed", True):
            return False

        result = optimise_uploaded_image(
            field_file.file,
            max_width=max_width,
            max_height=max_height,
            quality=quality,
            minimum_quality=minimum_quality,
            target_bytes=target_bytes,
            filename_prefix=filename_prefix,
        )

        setattr(self, field_name, result.content)
        return True

    def save(self, *args, **kwargs):
        processed_fields = set()

        desktop_processed = self._optimise_new_upload(
            "image",
            max_width=1920,
            max_height=1200,
            target_bytes=550_000,
            quality=82,
            minimum_quality=62,
            filename_prefix="home-slide",
        )

        if desktop_processed:
            processed_fields.add("image")

        mobile_processed = self._optimise_new_upload(
            "mobile_image",
            max_width=900,
            max_height=1600,
            target_bytes=300_000,
            quality=80,
            minimum_quality=60,
            filename_prefix="home-slide-mobile",
        )

        if mobile_processed:
            processed_fields.add("mobile_image")

        # Preserve explicitly supplied update_fields while ensuring newly
        # processed image fields are included in the database update.
        if processed_fields and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | processed_fields

        super().save(*args, **kwargs)

    class Meta:
        ordering = ("display_order", "id")

    def __str__(self):
        return self.title

    @property
    def is_current(self):
        now = timezone.now()
        if not self.is_active:
            return False
        if self.starts_at and self.starts_at > now:
            return False
        if self.ends_at and self.ends_at < now:
            return False
        return True


def generate_enquiry_reference():
    date_part = timezone.localtime().strftime("%Y%m%d")
    return f"PV-{date_part}-{secrets.token_hex(8).upper()}"


class Enquiry(models.Model):
    class EmailStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    reference = models.CharField(max_length=32, unique=True, editable=False)
    name = models.CharField(max_length=120)
    email = models.EmailField()
    phone = models.CharField(max_length=50, blank=True)
    message = models.TextField()
    source_url = models.URLField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    email_status = models.CharField(
        max_length=16,
        choices=EmailStatus.choices,
        default=EmailStatus.PENDING,
    )
    email_sent_at = models.DateTimeField(blank=True, null=True)
    email_error = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Enquiries"

    def __str__(self):
        return self.reference

    def save(self, *args, **kwargs):
        if not self._state.adding:
            self.reference = (
                type(self).objects.only("reference").get(pk=self.pk).reference
            )
            return super().save(*args, **kwargs)

        for _ in range(10):
            candidate = generate_enquiry_reference()
            self.reference = candidate
            try:
                # The nested atomic block creates a savepoint when save() is called
                # inside a wider transaction. A uniqueness failure can therefore be
                # retried without leaving that transaction unusable.
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if not type(self).objects.filter(reference=candidate).exists():
                    raise
                self.pk = None
                self._state.adding = True

        raise RuntimeError("Could not generate a unique enquiry reference")


class ContactRateLimitBucket(models.Model):
    identity_hash = models.CharField(max_length=64, unique=True, db_index=True)
    window_started_at = models.DateTimeField()
    request_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.identity_hash
