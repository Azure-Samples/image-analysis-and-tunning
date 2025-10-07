"""Singleton-based utility hook for the analysis service."""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import tempfile
import threading
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException, UploadFile

from .api_models import ApiError, ErrorResponse


class _SingletonMeta(type):
    """Thread-safe singleton metaclass."""

    _instances: Dict[type, Any] = {}
    _lock: threading.Lock = threading.Lock()

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class AnalysisHook(metaclass=_SingletonMeta):
    """Utility hook container for the analysis service."""

    _IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}

    def __init__(self) -> None:
        self.logger = logging.getLogger("analysis.hook")
        if not self.logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)

        self._file_lock = threading.RLock()
        self._auth_token = os.getenv("ANALYSIS_AUTH_TOKEN")
        if self._auth_token:
            self.logger.debug("Loaded ANALYSIS_AUTH_TOKEN for outbound requests")
        else:
            self.logger.warning(
                "ANALYSIS_AUTH_TOKEN not found; proceeding without explicit token auth"
            )

    def parse_csv_env(self, name: str, fallback: Iterable[str]) -> List[str]:
        raw = os.getenv(name, "")
        if not raw:
            return list(fallback)
        parsed = [item.strip() for item in raw.split(",") if item.strip()]
        self.logger.debug("Parsed CSV env %s -> %s", name, parsed)
        return parsed

    def build_error_exception(
        self,
        status_code: int,
        *,
        code: str,
        message: str,
        action: str,
        details: Optional[Any] = None,
    ) -> HTTPException:
        body = ErrorResponse(
            error=ApiError(code=code, message=message, details=details, action=action)
        )
        return HTTPException(status_code=status_code, detail=body.model_dump())

    def ensure_size_limit(self, size: int, limit_mb: int = 10) -> None:
        if size > limit_mb * 1024 * 1024:
            raise self.build_error_exception(
                413,
                code="payload_too_large",
                message=f"Image exceeds the allowed {limit_mb} MB limit",
                action="Resize or compress the image before retrying",
            )

    def cleanup_temp_file(self, path: str) -> None:
        with self._file_lock:
            try:
                pathlib.Path(path).unlink(missing_ok=True)
            except Exception as exc:  # pragma: no cover - best effort cleanup
                self.logger.debug("Failed to delete temp file %s: %s", path, exc)

    async def persist_upload_temporarily(self, upload: UploadFile) -> str:
        suffix = pathlib.Path(upload.filename or "upload.bin").suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            while True:
                chunk = await upload.read(1 << 16)
                if not chunk:
                    break
                tmp.write(chunk)
            temp_path = tmp.name
        await upload.close()
        file_size = pathlib.Path(temp_path).stat().st_size
        self.ensure_size_limit(file_size)
        self.logger.debug("Persisted upload to %s (%s bytes)", temp_path, file_size)
        return temp_path

    def is_image_file(self, path: str) -> bool:
        ext = pathlib.Path(path).suffix.lower()
        return ext in self._IMAGE_EXTENSIONS

    def get_auth_headers(self) -> Dict[str, str]:
        """Return authorization headers if a token is configured."""

        if not self._auth_token:
            return {}
        return {"Authorization": f"Bearer {self._auth_token}"}


def get_analysis_hook() -> AnalysisHook:
    """Public accessor for the singleton hook."""

    return AnalysisHook()


__all__ = ["AnalysisHook", "get_analysis_hook"]
