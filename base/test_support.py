"""Fresh STORAGES dictionaries for tests.

Callers must not mutate the returned mapping or Django's live settings dict.
"""

FILESYSTEM_BACKEND = "django.core.files.storage.FileSystemStorage"
PLAIN_STATIC_BACKEND = "django.contrib.staticfiles.storage.StaticFilesStorage"
WHITENOISE_MANIFEST_BACKEND = (
    "whitenoise.storage.CompressedManifestStaticFilesStorage"
)


def isolated_storages(
    *,
    default_backend=FILESYSTEM_BACKEND,
    staticfiles_backend=WHITENOISE_MANIFEST_BACKEND,
):
    return {
        "default": {"BACKEND": default_backend},
        "staticfiles": {"BACKEND": staticfiles_backend},
    }
