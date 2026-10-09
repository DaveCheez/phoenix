"""DEBUG and non-DEBUG storage selection.

These checks load base.settings in a clean subprocess so they do not depend
on the test runner's settings module or a previous storage instance.
"""

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FILESYSTEM = "django.core.files.storage.FileSystemStorage"
SPACES = "cdn.backends.MediaRootS3BotoStorage"
WHITENOISE = "whitenoise.storage.CompressedManifestStaticFilesStorage"


def _clean_env(debug_setting):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "DJANGO_SETTINGS_MODULE": "base.settings",
        "DEBUG_SETTING": debug_setting,
        "DJANGO_SECRET_KEY": "storage-selection-test-only-not-a-production-secret",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_SHARED_CREDENTIALS_FILE": "/aws-missing/credentials",
        "AWS_CONFIG_FILE": "/aws-missing/config",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return env


def _load_storages(debug_setting):
    script = textwrap.dedent(
        """
        import django
        from django.conf import settings

        settings.DATABASES["default"] = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
        django.setup()
        from cdn.backends import MediaRootS3BotoStorage

        print(settings.STORAGES["default"]["BACKEND"])
        print(settings.STORAGES["staticfiles"]["BACKEND"])
        if settings.DEBUG:
            raise SystemExit(0)
        print(settings.AWS_S3_FILE_OVERWRITE)
        print(settings.AWS_QUERYSTRING_AUTH)
        print(settings.AWS_DEFAULT_ACL)
        print(settings.AWS_STORAGE_BUCKET_NAME)
        print(settings.AWS_S3_ENDPOINT_URL)
        print(settings.AWS_S3_REGION_NAME)
        print(repr(settings.AWS_S3_CUSTOM_DOMAIN))
        print(settings.AWS_S3_OBJECT_PARAMETERS["CacheControl"])
        print(MediaRootS3BotoStorage.location)
        print("aws_access_key_empty=" + str(settings.AWS_ACCESS_KEY_ID == ""))
        print("aws_secret_empty=" + str(settings.AWS_SECRET_ACCESS_KEY == ""))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=_clean_env(debug_setting),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "Storage settings subprocess failed.\n"
            + completed.stdout
            + completed.stderr
        )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return lines


class StorageSelectionTests(unittest.TestCase):
    def test_debug_uses_local_media_and_whitenoise_static(self):
        lines = _load_storages("True")
        self.assertEqual(lines[0], FILESYSTEM)
        self.assertEqual(lines[1], WHITENOISE)
        self.assertNotEqual(lines[0], SPACES)

    def test_production_media_uses_spaces_and_keeps_whitenoise(self):
        lines = _load_storages("False")
        self.assertEqual(lines[0], SPACES)
        self.assertEqual(lines[1], WHITENOISE)
        self.assertNotIn(FILESYSTEM, lines[0])
        self.assertEqual(lines[2], "False")
        self.assertEqual(lines[3], "False")
        self.assertEqual(lines[4], "public-read")
        self.assertEqual(lines[5], "phoenixvanz")
        self.assertEqual(lines[6], "https://fra1.digitaloceanspaces.com")
        self.assertEqual(lines[7], "fra1")
        self.assertEqual(lines[8], "None")
        self.assertEqual(lines[9], "max-age=86400")
        self.assertEqual(lines[10], "media")
        self.assertEqual(lines[11], "aws_access_key_empty=True")
        self.assertEqual(lines[12], "aws_secret_empty=True")
        self.assertFalse((ROOT / "db.sqlite3").exists())
