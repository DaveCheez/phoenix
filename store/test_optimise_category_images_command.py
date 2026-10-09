from io import BytesIO, StringIO
from tempfile import TemporaryDirectory

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.test import TestCase, override_settings
from PIL import Image

from base.test_support import isolated_storages

from .models import Category, CategoryImage


def make_image_bytes(
    *,
    size=(4000, 3000),
    image_format="JPEG",
    colour=(80, 130, 180),
):
    image = Image.new("RGB", size, colour)
    output = BytesIO()
    image.save(output, format=image_format, quality=95)
    return output.getvalue()


class OptimiseCategoryImagesCommandTests(TestCase):
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

    def create_legacy_image(
        self,
        *,
        slug,
        filename,
        image_type="thumbnail",
        size=(4000, 3000),
    ):
        """
        Store a legacy image without calling CategoryImage.save().

        bulk_create() deliberately bypasses the new upload optimiser so the
        command can be tested against an existing JPEG record.
        """
        storage_name = default_storage.save(
            f"category_images/{filename}",
            ContentFile(
                make_image_bytes(size=size),
                name=filename,
            ),
        )

        CategoryImage.objects.bulk_create(
            [
                CategoryImage(
                    category=self.category,
                    name=slug.replace("-", " ").title(),
                    slug=slug,
                    image_type=image_type,
                    image=storage_name,
                )
            ]
        )

        return CategoryImage.objects.get(slug=slug)

    def test_command_optimises_existing_thumbnail(self):
        category_image = self.create_legacy_image(
            slug="legacy-thumbnail",
            filename="legacy-thumbnail.jpg",
        )

        original_name = category_image.image.name
        original_size = category_image.image.size
        stdout = StringIO()

        call_command(
            "optimise_category_images",
            stdout=stdout,
        )

        category_image.refresh_from_db()

        self.assertNotEqual(
            category_image.image.name,
            original_name,
        )
        self.assertTrue(
            category_image.image.name.endswith(".webp")
        )
        self.assertIn(
            "category_images/category-thumbnail-",
            category_image.image.name,
        )
        self.assertLess(
            category_image.image.size,
            original_size,
        )

        category_image.image.open("rb")

        with Image.open(category_image.image) as optimised:
            self.assertEqual(optimised.format, "WEBP")
            self.assertLessEqual(optimised.width, 1200)
            self.assertLessEqual(optimised.height, 900)

        category_image.image.close()

        # Existing objects remain in storage as temporary rollback copies.
        self.assertTrue(default_storage.exists(original_name))
        self.assertIn("Optimised ID", stdout.getvalue())

    def test_dry_run_does_not_change_file_or_database(self):
        category_image = self.create_legacy_image(
            slug="dry-run-thumbnail",
            filename="dry-run-thumbnail.jpg",
        )

        original_name = category_image.image.name
        stdout = StringIO()

        call_command(
            "optimise_category_images",
            dry_run=True,
            stdout=stdout,
        )

        category_image.refresh_from_db()

        self.assertEqual(
            category_image.image.name,
            original_name,
        )
        self.assertIn("DRY RUN", stdout.getvalue())
        self.assertIn("Would optimise ID", stdout.getvalue())

    def test_ids_option_only_processes_selected_records(self):
        selected = self.create_legacy_image(
            slug="selected-thumbnail",
            filename="selected-thumbnail.jpg",
        )
        unselected = self.create_legacy_image(
            slug="unselected-thumbnail",
            filename="unselected-thumbnail.jpg",
        )

        selected_original = selected.image.name
        unselected_original = unselected.image.name

        call_command(
            "optimise_category_images",
            ids=[selected.id],
        )

        selected.refresh_from_db()
        unselected.refresh_from_db()

        self.assertNotEqual(
            selected.image.name,
            selected_original,
        )
        self.assertEqual(
            unselected.image.name,
            unselected_original,
        )

    def test_generated_webp_is_skipped_on_later_runs(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        source = SimpleUploadedFile(
            "new-thumbnail.jpg",
            make_image_bytes(size=(2400, 1600)),
            content_type="image/jpeg",
        )

        category_image = CategoryImage.objects.create(
            category=self.category,
            name="New thumbnail",
            slug="new-thumbnail",
            image_type="thumbnail",
            image=source,
        )

        generated_name = category_image.image.name
        stdout = StringIO()

        call_command(
            "optimise_category_images",
            stdout=stdout,
        )

        category_image.refresh_from_db()

        self.assertEqual(
            category_image.image.name,
            generated_name,
        )
        self.assertIn("already optimised", stdout.getvalue())