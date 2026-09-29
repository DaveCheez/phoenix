from rest_framework import serializers

from .models import (
    Category,
    Enquiry,
    HomeSlide,
    Product,
    ProductImage,
    ProductOption,
    ProductOptionGroup,
    Review,
)


def absolute_image_url(request, image_field):
    if not image_field:
        return None

    url = image_field.url
    if url.startswith(("http://", "https://")):
        return url
    return request.build_absolute_uri(url) if request else url


class ProductOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductOption
        fields = ["id", "name", "price", "sku"]


class ProductOptionGroupSerializer(serializers.ModelSerializer):
    options = ProductOptionSerializer(many=True, read_only=True)

    class Meta:
        model = ProductOptionGroup
        fields = ["id", "name", "required", "options"]


class ProductImageSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = ["id", "name", "image", "slug", "image_type"]

    def get_image(self, obj):
        return absolute_image_url(self.context.get("request"), obj.image)


class ProductSerializer(serializers.ModelSerializer):
    option_groups = ProductOptionGroupSerializer(many=True, read_only=True)
    productimage_set = ProductImageSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "price",
            "option_groups",
            "productimage_set",
        ]


class CategorySerializer(serializers.ModelSerializer):
    header_image = serializers.SerializerMethodField()
    thumbnail_image = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "header_image",
            "thumbnail_image",
        ]

    def get_header_image(self, obj):
        image = obj.categoryimage_set.filter(image_type="header").first()
        return absolute_image_url(self.context.get("request"), image.image) if image else None

    def get_thumbnail_image(self, obj):
        image = obj.categoryimage_set.filter(image_type="thumbnail").first()
        return absolute_image_url(self.context.get("request"), image.image) if image else None


class ProductListSerializer(serializers.ModelSerializer):
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ["id", "name", "slug", "description", "price", "thumbnail"]

    def get_thumbnail(self, obj):
        image = (
            obj.productimage_set.filter(image_type="thumbnail").first()
            or obj.productimage_set.first()
        )
        return absolute_image_url(self.context.get("request"), image.image) if image else None


class CategoryWithProductsSerializer(serializers.ModelSerializer):
    products = serializers.SerializerMethodField()
    header_image = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "description", "header_image", "products"]

    def get_products(self, obj):
        return ProductListSerializer(
            obj.store.all(),
            many=True,
            context=self.context,
        ).data

    def get_header_image(self, obj):
        image = obj.categoryimage_set.filter(image_type="header").first()
        return absolute_image_url(self.context.get("request"), image.image) if image else None


class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = ["id", "name", "content", "created_at", "stars"]


class HomeSlideSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()
    mobile_image = serializers.SerializerMethodField()

    class Meta:
        model = HomeSlide
        fields = [
            "id",
            "title",
            "subtitle",
            "image",
            "mobile_image",
            "button_text",
            "button_url",
            "display_order",
        ]

    def get_image(self, obj):
        return absolute_image_url(self.context.get("request"), obj.image)

    def get_mobile_image(self, obj):
        return absolute_image_url(self.context.get("request"), obj.mobile_image)


class ContactEnquirySerializer(serializers.ModelSerializer):
    website = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        max_length=200,
    )
    phone = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=50,
        trim_whitespace=True,
    )
    source_url = serializers.URLField(
        required=False,
        allow_blank=True,
        max_length=500,
    )

    class Meta:
        model = Enquiry
        fields = [
            "name",
            "email",
            "phone",
            "message",
            "source_url",
            "website",
        ]
        extra_kwargs = {
            "name": {"max_length": 120, "trim_whitespace": True},
            "email": {"trim_whitespace": True},
            "message": {"max_length": 5000, "trim_whitespace": True},
        }

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = set(data.keys()) - set(self.fields)
            if unknown:
                raise serializers.ValidationError(
                    {
                        field: ["This field is not supported."]
                        for field in sorted(unknown)
                    }
                )
        return super().to_internal_value(data)

    def validate_website(self, value):
        if value:
            raise serializers.ValidationError("This field must be empty.")
        return value

    def validate_name(self, value):
        if "\r" in value or "\n" in value:
            raise serializers.ValidationError(
                "Name cannot contain line breaks."
            )
        return value

    def create(self, validated_data):
        validated_data.pop("website", None)
        return Enquiry.objects.create(**validated_data)
