"""Inspect bounded raster inputs without writing uploaded data to disk."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, UnidentifiedImageError

FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def media_type(data: bytes) -> str:
    try:
        with Image.open(BytesIO(data), formats=tuple(FORMATS)) as image:
            width, height = image.size
            if (
                not 1 <= min(width, height)
                or max(width, height) > 4096
                or width * height > 12_000_000
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ValueError("Unsupported image dimensions or animation")
            mime = FORMATS[image.format]
            image.verify()
        with Image.open(BytesIO(data), formats=tuple(FORMATS)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("Invalid image") from error
    return mime
