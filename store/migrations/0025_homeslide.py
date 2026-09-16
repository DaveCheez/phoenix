from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("store", "0024_productimage_image_type_alter_productimage_image"),
    ]

    operations = [
        migrations.CreateModel(
            name="HomeSlide",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("title", models.CharField(default="Phoenix Vanz", max_length=160)),
                ("subtitle", models.CharField(blank=True, max_length=255)),
                ("image", models.ImageField(upload_to="home_slides/")),
                (
                    "mobile_image",
                    models.ImageField(
                        blank=True,
                        help_text="Optional portrait or mobile-specific image.",
                        null=True,
                        upload_to="home_slides/mobile/",
                    ),
                ),
                ("button_text", models.CharField(blank=True, max_length=80)),
                (
                    "button_url",
                    models.CharField(
                        blank=True,
                        help_text="Use a relative path such as /shop/roof-racks or a full URL.",
                        max_length=500,
                    ),
                ),
                ("display_order", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("starts_at", models.DateTimeField(blank=True, null=True)),
                ("ends_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ("display_order", "id"),
            },
        ),
    ]
