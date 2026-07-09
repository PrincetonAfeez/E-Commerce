from __future__ import annotations

import os

from django.conf import settings
from django.core.exceptions import ValidationError

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def validate_image_upload(file) -> None:
    """Validate uploaded product images by extension and size (spec §9/§24.3).

    Content-type sniffing is handled by Pillow (ImageField), which rejects non-images;
    here we bound the size and restrict to a known-safe extension allow-list.
    """
    max_size = getattr(settings, "MAX_UPLOAD_SIZE_BYTES", 5 * 1024 * 1024)
    if file.size and file.size > max_size:
        raise ValidationError(
            f"Image is too large ({file.size} bytes). Maximum allowed is {max_size} bytes."
        )
    ext = os.path.splitext(file.name)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_IMAGE_EXTENSIONS))
        raise ValidationError(f"Unsupported image type '{ext}'. Allowed: {allowed}.")
