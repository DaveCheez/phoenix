from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError


@dataclass(frozen=True)
class OptimisedImage:
    """Result returned after an uploaded image has been processed."""

    content: ContentFile
    width: int
    height: int
    byte_size: int
    quality: int


def _normalise_mode(image: Image.Image) -> Image.Image:
    """
    Convert the image to a WebP-compatible mode.

    Transparent PNG-style images retain their alpha channel. Photographic and
    other opaque images are converted to RGB.
    """
    if image.mode in {"RGBA", "LA"}:
        return image.convert("RGBA")

    if image.mode == "P" and "transparency" in image.info:
        return image.convert("RGBA")

    return image.convert("RGB")


def _validate_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive integers.")


def optimise_uploaded_image(
    uploaded_file: BinaryIO,
    *,
    max_width: int,
    max_height: int,
    quality: int = 82,
    minimum_quality: int = 62,
    target_bytes: int | None = None,
    crop_width: int | None = None,
    crop_height: int | None = None,
    filename_prefix: str | None = None,
) -> OptimisedImage:
    """
    Correct, resize and convert an uploaded image to WebP.

    The target byte size is best-effort. WebP quality is progressively reduced
    until the target is met or minimum_quality is reached.

    When crop_width and crop_height are supplied, the image is centre-cropped
    to those exact dimensions. Otherwise, its aspect ratio is preserved and it
    is resized to fit inside max_width x max_height.
    """
    _validate_dimensions(max_width, max_height)

    if (crop_width is None) != (crop_height is None):
        raise ValueError(
            "crop_width and crop_height must either both be supplied or both "
            "be omitted."
        )

    if crop_width is not None and crop_height is not None:
        _validate_dimensions(crop_width, crop_height)

    if not 1 <= minimum_quality <= 100:
        raise ValueError("minimum_quality must be between 1 and 100.")

    if not minimum_quality <= quality <= 100:
        raise ValueError(
            "quality must be between minimum_quality and 100."
        )

    try:
        uploaded_file.seek(0)

        with Image.open(uploaded_file) as source:
            source.load()

            # Correct orientation from phone/camera EXIF metadata.
            image = ImageOps.exif_transpose(source)
            image = _normalise_mode(image)

    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationError(
            "The uploaded file is not a valid supported image."
        ) from exc

    if crop_width is not None and crop_height is not None:
        image = ImageOps.fit(
            image,
            (crop_width, crop_height),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    else:
        # thumbnail() never enlarges an image smaller than the requested size.
        image.thumbnail(
            (max_width, max_height),
            resample=Image.Resampling.LANCZOS,
            reducing_gap=3.0,
        )

    selected_quality = quality
    encoded = b""

    while True:
        output = BytesIO()

        image.save(
            output,
            format="WEBP",
            quality=selected_quality,
            method=6,
            exact=image.mode == "RGBA",
        )

        encoded = output.getvalue()

        if (
            target_bytes is None
            or len(encoded) <= target_bytes
            or selected_quality <= minimum_quality
        ):
            break

        selected_quality = max(minimum_quality, selected_quality - 5)

    original_name = getattr(uploaded_file, "name", "image")
    original_stem = Path(original_name).stem or "image"
    safe_prefix = filename_prefix or original_stem

    filename = f"{safe_prefix}-{uuid4().hex[:12]}.webp"

    content = ContentFile(encoded, name=filename)

    return OptimisedImage(
        content=content,
        width=image.width,
        height=image.height,
        byte_size=len(encoded),
        quality=selected_quality,
    )