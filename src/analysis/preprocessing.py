"""Image preprocessing utilities for the analysis service."""
from __future__ import annotations

import logging
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps
from rembg import remove

LOGGER = logging.getLogger("analysis.preprocessing")
LOGGER.setLevel(logging.INFO)

TARGET_RATIO = 3 / 4
MAX_HEIGHT = 1200
JPEG_QUALITY = 95


_RESAMPLING_NAMESPACE = getattr(Image, "Resampling", None)
_DEFAULT_RESAMPLE = getattr(
    Image,
    "LANCZOS",
    getattr(Image, "BICUBIC", getattr(Image, "NEAREST", 1)),
)


def _get_resample_filter() -> int:
    if _RESAMPLING_NAMESPACE is not None:
        return getattr(_RESAMPLING_NAMESPACE, "LANCZOS", _DEFAULT_RESAMPLE)
    return _DEFAULT_RESAMPLE


RESAMPLE_FILTER = _get_resample_filter()


def _crop_to_ratio(image: Image.Image, ratio: float) -> Image.Image:
    width, height = image.size
    if width == 0 or height == 0:
        return image

    current_ratio = width / height
    if abs(current_ratio - ratio) <= 0.01:
        return image

    if current_ratio > ratio:
        target_width = min(width, max(1, round(height * ratio)))
        offset = max(0, (width - target_width) // 2)
        box = (offset, 0, offset + target_width, height)
    else:
        target_height = min(height, max(1, round(width / ratio)))
        offset = max(0, (height - target_height) // 2)
        box = (0, offset, width, offset + target_height)

    return image.crop(box)


def _downscale_if_needed(image: Image.Image, ratio: float) -> Image.Image:
    width, height = image.size
    if height <= MAX_HEIGHT:
        return image

    new_height = MAX_HEIGHT
    new_width = max(1, round(new_height * ratio))
    return image.resize((new_width, new_height), RESAMPLE_FILTER)


def preprocess_image(input_path: str) -> str:
    """Remove the background, enforce a 3x4 ratio and export to JPEG.

    Args:
        input_path: Path to the user-provided image.

    Returns:
        Path to a new temporary JPEG containing the processed image.
    """

    source_path = Path(input_path)
    if not source_path.exists():
        raise FileNotFoundError(f"Input image not found: {input_path}")

    with source_path.open("rb") as file:
        source_bytes = file.read()

    try:
        processed_output = remove(source_bytes)
        if isinstance(processed_output, Image.Image):
            image = processed_output
        elif isinstance(processed_output, (bytes, bytearray, memoryview)):
            image = Image.open(BytesIO(bytes(processed_output)))
        else:
            LOGGER.warning("Unexpected rembg output type %s; using original bytes", type(processed_output))
            image = Image.open(BytesIO(source_bytes))
        LOGGER.debug("Applied background removal to %s", input_path)
    except Exception as exc:  # pragma: no cover - best effort fallback
        LOGGER.warning("Background removal failed for %s: %s", input_path, exc)
        image = Image.open(BytesIO(source_bytes))

    with image:
        image = ImageOps.exif_transpose(image).convert("RGBA")
        image.load()

        cropped = _crop_to_ratio(image, TARGET_RATIO)
        resized = _downscale_if_needed(cropped, TARGET_RATIO)

        background = Image.new("RGBA", resized.size, (255, 255, 255, 255))
        alpha = resized.split()[-1] if resized.mode == "RGBA" else None
        background.paste(resized, mask=alpha)
        final_image = background.convert("RGB")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            final_image.save(temp_file, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            output_path = temp_file.name

    LOGGER.info("Preprocessed image saved to %s (original %s)", output_path, input_path)
    return output_path


__all__ = ["preprocess_image"]
