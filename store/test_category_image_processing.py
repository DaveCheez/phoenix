from io import BytesIO
from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from base.test_support import isolated_storages

from .models import Category, CategoryImage


def make_uploaded_image(
    name,
    *,
    size,
    image_format="JPEG",
    colour=(70, 120, 170),
):
    image = Image.new("RGB", size, colour)
    output = BytesIO()
    image.save(output, format=image_format, quality=95)

    content_type = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }.get(image_format, "application/octet-stream")

    return SimpleUploadedFile(
        name,
        output.getvalue(),
        content_type=content_type,
    )


class CategoryImageProcessingTests(TestCase):
    def setUp(self):
        self.media_directory = TemporaryDirectory()

        self.storage_override = override_settings(
            MEDIA_ROOT=self.media_directory.name,
            STORAGES=isolated_storages(),
        )
        self.storage_override.enable()

        self.category = Category.objects.create(
            name="Roof Racks",
            slug="roof-racks",
        )

    def tearDown(self):
        self.storage_override.disable()
        self.media_directory.cleanup()

    def test_thumbnail_upload_is_resized_and_converted_to_webp(self):
        category_image = CategoryImage.objects.create(
            category=self.category,
            name="Roof rack thumbnail",
            slug="roof-rack-thumbnail",
            image_type="thumbnail",
            image=make_uploaded_image(
                "roof-rack.jpg",
                size=(4000, 3000),
            ),
        )

        self.assertTrue(category_image.image.name.endswith(".webp"))
        self.assertIn(
            "category_images/category-thumbnail-",
            category_image.image.name,
        )

        category_image.image.open("rb")

        with Image.open(category_image.image) as optimised:
            self.assertEqual(optimised.format, "WEBP")
            self.assertLessEqual(optimised.width, 1200)
            self.assertLessEqual(optimised.height, 900)

        category_image.image.close()

    def test_header_upload_retains_larger_dimensions(self):
        category_image = CategoryImage.objects.create(
            category=self.category,
            name="Roof rack header",
            slug="roof-rack-header",
            image_type="header",
            image=make_uploaded_image(
                "roof-rack-header.jpg",
                size=(4000, 2400),
            ),
        )

        self.assertTrue(category_image.image.name.endswith(".webp"))
        self.assertIn(
            "category_images/category-header-",
            category_image.image.name,
        )

        category_image.image.open("rb")

        with Image.open(category_image.image) as optimised:
            self.assertEqual(optimised.format, "WEBP")
            self.assertLessEqual(optimised.width, 1920)
            self.assertLessEqual(optimised.height, 1200)

            # A header may legitimately be larger than the thumbnail ceiling.
            self.assertGreater(optimised.width, 1200)

        category_image.image.close()

    def test_unspecified_image_uses_thumbnail_limits(self):
        category_image = CategoryImage.objects.create(
            category=self.category,
            name="Legacy category image",
            slug="legacy-category-image",
            image_type="",
            image=make_uploaded_image(
                "legacy.jpg",
                size=(3000, 2000),
            ),
        )

        category_image.image.open("rb")

        with Image.open(category_image.image) as optimised:
            self.assertEqual(optimised.format, "WEBP")
            self.assertLessEqual(optimised.width, 1200)
            self.assertLessEqual(optimised.height, 900)

        category_image.image.close()

    def test_editing_metadata_does_not_recompress_image(self):
        category_image = CategoryImage.objects.create(
            category=self.category,
            name="Original name",
            slug="original-name",
            image_type="thumbnail",
            image=make_uploaded_image(
                "thumbnail.jpg",
                size=(2400, 1600),
            ),
        )

        original_filename = category_image.image.name
        original_size = category_image.image.size

        category_image.name = "Updated name"
        category_image.save()

        category_image.refresh_from_db()

        self.assertEqual(category_image.image.name, original_filename)
        self.assertEqual(category_image.image.size, original_size)

        