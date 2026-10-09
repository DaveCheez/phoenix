from io import BytesIO
from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from base.test_support import isolated_storages

from .models import HomeSlide


def make_uploaded_image(
    name,
    *,
    size,
    image_format="JPEG",
    colour=(60, 110, 160),
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


class HomeSlideImageProcessingTests(TestCase):
    def setUp(self):
        self.media_directory = TemporaryDirectory()

        self.storage_override = override_settings(
            MEDIA_ROOT=self.media_directory.name,
            STORAGES=isolated_storages(),
        )
        self.storage_override.enable()

    def tearDown(self):
        self.storage_override.disable()
        self.media_directory.cleanup()

    def test_new_desktop_and_mobile_uploads_are_optimised(self):
        slide = HomeSlide.objects.create(
            title="Optimised slide",
            image=make_uploaded_image(
                "desktop.jpg",
                size=(4000, 3000),
            ),
            mobile_image=make_uploaded_image(
                "mobile.jpg",
                size=(2400, 4000),
            ),
        )

        self.assertTrue(slide.image.name.endswith(".webp"))
        self.assertTrue(slide.mobile_image.name.endswith(".webp"))

        self.assertIn("home_slides/home-slide-", slide.image.name)
        self.assertIn(
            "home_slides/mobile/home-slide-mobile-",
            slide.mobile_image.name,
        )

        slide.image.open("rb")
        with Image.open(slide.image) as desktop:
            self.assertEqual(desktop.format, "WEBP")
            self.assertLessEqual(desktop.width, 1920)
            self.assertLessEqual(desktop.height, 1200)

        slide.image.close()

        slide.mobile_image.open("rb")
        with Image.open(slide.mobile_image) as mobile:
            self.assertEqual(mobile.format, "WEBP")
            self.assertLessEqual(mobile.width, 900)
            self.assertLessEqual(mobile.height, 1600)

        slide.mobile_image.close()

    def test_editing_slide_metadata_does_not_recompress_images(self):
        slide = HomeSlide.objects.create(
            title="Original title",
            image=make_uploaded_image(
                "desktop.jpg",
                size=(2400, 1600),
            ),
            mobile_image=make_uploaded_image(
                "mobile.jpg",
                size=(900, 1600),
            ),
        )

        original_desktop_name = slide.image.name
        original_mobile_name = slide.mobile_image.name

        slide.title = "Updated title"
        slide.display_order = 20
        slide.save()

        slide.refresh_from_db()

        self.assertEqual(slide.image.name, original_desktop_name)
        self.assertEqual(
            slide.mobile_image.name,
            original_mobile_name,
        )

    def test_mobile_image_remains_optional(self):
        slide = HomeSlide.objects.create(
            title="Desktop only",
            image=make_uploaded_image(
                "desktop.jpg",
                size=(3000, 2000),
            ),
        )

        self.assertTrue(slide.image.name.endswith(".webp"))
        self.assertFalse(slide.mobile_image)

        