"""Image preprocessing utilities for the analysis service."""
from __future__ import annotations

import logging
import os
import threading
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps
from rembg import new_session, remove

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

_SESSION_LOCK = threading.Lock()
_SESSION = None


def _get_rembg_session():
    global _SESSION
    if _SESSION is not None:
        return _SESSION

    with _SESSION_LOCK:
        if _SESSION is not None:
            return _SESSION

        model_name = os.getenv("REMBG_SESSION", "u2net_human_seg")
        try:
            session = new_session(model_name)
            LOGGER.info("Initialized rembg session with model '%s'", model_name)
            _SESSION = session
        except Exception as exc:  # pragma: no cover - don't fail pipeline if model fetch fails
            LOGGER.warning(
                "Failed to initialize rembg session '%s': %s. Falling back to default session.",
                model_name,
                exc,
            )
            _SESSION = None
    return _SESSION


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

    if os.getenv("ANALYSIS_DISABLE_BACKGROUND_REMOVAL", "false").lower() in {"1", "true", "yes"}:
        LOGGER.info("Background removal disabled via ANALYSIS_DISABLE_BACKGROUND_REMOVAL")
        return input_path

    source_path = Path(input_path)
    if not source_path.exists():
        raise FileNotFoundError(f"Input image not found: {input_path}")

    with source_path.open("rb") as file:
        source_bytes = file.read()

    session = _get_rembg_session()

    try:
        processed_output = remove(source_bytes, session=session)
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

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temp_file:
            final_image.save(temp_file, format="PNG", optimize=True)
            output_path = temp_file.name

    LOGGER.info("Preprocessed image saved to %s (original %s)", output_path, input_path)
    return output_path


__all__ = ["preprocess_image"]
