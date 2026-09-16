from django.db import models
from django.urls import reverse
from django.conf import settings
from django.utils import timezone



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

    def __str__(self):
        return f"{self.product.name} - {self.name} ({'Required' if self.required else 'Optional'})"


class ProductOption(models.Model):
    group = models.ForeignKey(ProductOptionGroup, related_name='options', on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=100)  # e.g. MWB, LWB, 2x Lights
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    sku = models.CharField(max_length=50, blank=True, null=True)

    def __str__(self):
        return f"{self.group.name} - {self.name} (£{self.price})"


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
