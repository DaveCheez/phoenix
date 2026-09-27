from io import BytesIO

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image

from .image_processing import optimise_uploaded_image


def make_uploaded_image(
    *,
    name: str,
    size: tuple[int, int],
    mode: str = "RGB",
    colour=None,
    image_format: str = "JPEG",
) -> SimpleUploadedFile:
    if colour is None:
        colour = (
            (51, 102, 153, 255)
            if mode == "RGBA"
            else (51, 102, 153)
        )

    image = Image.new(mode, size, colour)
    output = BytesIO()
    image.save(output, format=image_format)

    content_type = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
    }.get(image_format, "application/octet-stream")

    return SimpleUploadedFile(
        name,
        output.getvalue(),
        content_type=content_type,
    )


class ImageProcessingTests(SimpleTestCase):
    def test_large_jpeg_is_resized_and_converted_to_webp(self):
        uploaded = make_uploaded_image(
            name="large-photo.jpg",
            size=(4000, 3000),
        )
        original_size = uploaded.size

        result = optimise_uploaded_image(
            uploaded,
            max_width=1920,
            max_height=1080,
            target_bytes=500_000,
        )

        self.assertTrue(result.content.name.endswith(".webp"))
        self.assertLessEqual(result.width, 1920)
        self.assertLessEqual(result.height, 1080)
        self.assertLess(result.byte_size, original_size)

        result.content.seek(0)

        with Image.open(result.content) as optimised:
            self.assertEqual(optimised.format, "WEBP")
            self.assertLessEqual(optimised.width, 1920)
            self.assertLessEqual(optimised.height, 1080)

    def test_small_image_is_not_enlarged(self):
        uploaded = make_uploaded_image(
            name="small-photo.jpg",
            size=(640, 480),
        )

        result = optimise_uploaded_image(
            uploaded,
            max_width=1920,
            max_height=1080,
        )

        self.assertEqual((result.width, result.height), (640, 480))

    def test_exact_crop_dimensions_are_supported(self):
        uploaded = make_uploaded_image(
            name="desktop-photo.jpg",
            size=(2400, 1600),
        )

        result = optimise_uploaded_image(
            uploaded,
            max_width=900,
            max_height=1600,
            crop_width=900,
            crop_height=1600,
        )

        self.assertEqual((result.width, result.height), (900, 1600))

    def test_transparency_is_preserved(self):
        uploaded = make_uploaded_image(
            name="transparent-logo.png",
            size=(300, 300),
            mode="RGBA",
            colour=(0, 0, 0, 0),
            image_format="PNG",
        )

        result = optimise_uploaded_image(
            uploaded,
            max_width=300,
            max_height=300,
        )

        result.content.seek(0)

        with Image.open(result.content) as optimised:
            self.assertEqual(optimised.format, "WEBP")
            self.assertIn("A", optimised.getbands())

    def test_invalid_file_is_rejected(self):
        uploaded = SimpleUploadedFile(
            "not-an-image.txt",
            b"This is not an image.",
            content_type="text/plain",
        )

        with self.assertRaises(ValidationError):
            optimise_uploaded_image(
                uploaded,
                max_width=1920,
                max_height=1080,
            )

    def test_invalid_crop_configuration_is_rejected(self):
        uploaded = make_uploaded_image(
            name="photo.jpg",
            size=(1200, 800),
        )

        with self.assertRaises(ValueError):
            optimise_uploaded_image(
                uploaded,
                max_width=1920,
                max_height=1080,
                crop_width=900,
            )

            