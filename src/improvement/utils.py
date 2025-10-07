"""Singleton-based utility hook for the improvement service."""
from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import os
import pathlib
import sys
import tempfile
import threading
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple, Literal, cast

from fastapi import HTTPException, UploadFile

from analysis.api_models import ApiError, ErrorResponse

try:  # pragma: no cover - optional dependency used when contacting URLs
    import requests  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - degrade gracefully if missing
    requests = None  # type: ignore[assignment]

if TYPE_CHECKING:  # pragma: no cover - type checking only
    from azure.ai.agents.models import (  # type: ignore[import-not-found]
        MessageImageFileParam,
        MessageInputImageFileBlock,
        MessageInputTextBlock,
    )
    from azure.ai.projects.aio import AIProjectClient  # type: ignore[import-not-found]
    from azure.identity.aio import DefaultAzureCredential  # type: ignore[import-not-found]
    from .schemas import ImageImprovementJob


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


class ImprovementHook(metaclass=_SingletonMeta):
    """Utility hook container for the improvement service."""

    _ALLOWED_SIZES = {"256x256", "512x512", "1024x1024"}

    def __init__(self) -> None:
        self.logger = logging.getLogger("improvement.hook")
        if not self.logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)

        self._file_lock = threading.RLock()
        self._auth_token = os.getenv("IMPROVEMENT_AUTH_TOKEN")
        if not self._auth_token:
            self.logger.warning(
                "IMPROVEMENT_AUTH_TOKEN not found; proceeding without explicit token auth"
            )
        else:
            self.logger.debug("Loaded IMPROVEMENT_AUTH_TOKEN for outbound requests")

        try:  # pragma: no cover - optional Azure dependencies
            from azure.ai.projects.aio import AIProjectClient  # type: ignore
            from azure.identity.aio import DefaultAzureCredential  # type: ignore
            from azure.ai.agents.models import (  # type: ignore
                MessageInputTextBlock,
                MessageImageFileParam,
                MessageInputImageFileBlock,
            )

            self.AIProjectClient = AIProjectClient
            self.DefaultAzureCredential = DefaultAzureCredential
            self.MessageInputTextBlock = MessageInputTextBlock
            self.MessageImageFileParam = MessageImageFileParam
            self.MessageInputImageFileBlock = MessageInputImageFileBlock
            self.logger.debug("Azure AI SDK components loaded successfully")
        except ImportError as exc:  # pragma: no cover - fallback path
            self.logger.warning(
                "Azure AI SDK packages not available. Image enhancement operations will be limited: %s",
                exc,
            )
            self.AIProjectClient = None
            self.DefaultAzureCredential = None
            self.MessageInputTextBlock = None
            self.MessageImageFileParam = None
            self.MessageInputImageFileBlock = None

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

    def ensure_size_limit(self, size: int, limit_mb: int = 20) -> None:
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
            except Exception as exc:  # pragma: no cover - best effort
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

    def parse_criteria_scores(self, raw_value: Optional[str]) -> Dict[str, int]:
        if not raw_value:
            return {}
        try:
            parsed = json.loads(raw_value)
        except ValueError as exc:
            raise self.build_error_exception(
                400,
                code="invalid_criteria_scores",
                message="criteria_scores must be a JSON object mapping rule names to integers",
                action="Provide a JSON object (e.g. {'fondo_blanco': 10})",
                details=str(exc),
            ) from exc
        if not isinstance(parsed, dict):
            raise self.build_error_exception(
                400,
                code="invalid_criteria_scores",
                message="criteria_scores must be a JSON object",
                action="Send an object with string keys and numeric values",
            )
        sanitized: Dict[str, int] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                raise self.build_error_exception(
                    400,
                    code="invalid_criteria_key",
                    message="criteria_scores keys must be strings",
                    action="Convert the rubric name to a string",
                )
            if not isinstance(value, (int, float)):
                raise self.build_error_exception(
                    400,
                    code="invalid_criteria_value",
                    message=f"criteria_scores['{key}'] must be numeric",
                    action="Provide a number between 0 and 100",
                )
            sanitized[key] = int(value)
        return sanitized

    def validate_output_size(self, size: str) -> str:
        value = size.strip()
        if value not in self._ALLOWED_SIZES:
            raise self.build_error_exception(
                400,
                code="invalid_size",
                message=f"size must be one of {sorted(self._ALLOWED_SIZES)}",
                action="Pick a supported output size",
            )
        return value

    def guess_mime(self, path: pathlib.Path) -> str:
        mt = mimetypes.guess_type(str(path))[0]
        if mt:
            return mt
        ext = path.suffix.lower()
        if ext in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if ext == ".png":
            return "image/png"
        return "application/octet-stream"

    def load_evaluations(self, path: pathlib.Path) -> List[Dict[str, Any]]:
        if not path.exists():
            self.logger.warning("evaluations.json not found at %s", path)
            return []
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            self.logger.error("Failed to read evaluations.json: %s", exc)
            return []
        if isinstance(data, list):
            return data
        self.logger.warning("evaluations.json did not contain a list payload")
        return []

    def derive_improvement_instructions(
        self, item: Dict[str, Any]
    ) -> Tuple[str, List[str]]:
        fixes: List[str] = []
        cs: Dict[str, int] = item.get("criteria_scores") or {}

        if cs.get("tamaño_3x4", 0) < 25:
            fixes.append("Ajustar el recorte a proporción exacta 3:4 (ancho:alto) sin deformaciones.")
        if cs.get("fondo_blanco", 0) < 25:
            fixes.append("Uniformizar el fondo a blanco puro (#FFFFFF), sin texturas ni sombras.")
        if cs.get("mirada_frontal_rostro_homogeneo", 0) < 20:
            fixes.append("Mirada frontal con cabeza centrada, rostro totalmente visible e iluminación homogénea.")
        if cs.get("sin_dientes_visibles", 0) < 10:
            fixes.append("Cerrar los labios; sin dientes visibles.")
        if cs.get("identificable_sin_obstrucciones", 0) < 20:
            fixes.append("Eliminar obstrucciones (mascarillas, gafas de sol, viseras, sombras fuertes u objetos).")

        if not fixes:
            notes = (item.get("notes") or "").lower()
            if "3x4" in notes or "3:4" in notes:
                fixes.append("Ajustar el recorte a proporción 3:4.")
            if "fondo" in notes and ("blanco" in notes or "no blanco" in notes):
                fixes.append("Uniformizar el fondo a blanco puro (#FFFFFF).")
            if any(token in notes for token in ("mirada", "frontal", "rostro")):
                fixes.append("Mirada frontal, rostro homogéneo y centrado.")
            if "diente" in notes:
                fixes.append("Cerrar los labios; sin dientes visibles.")
            if any(token in notes for token in ("obstru", "gafa de sol", "mascar")):
                fixes.append("Eliminar obstrucciones para una identificación clara.")

        if not fixes:
            fixes.append(
                "Mejorar sutilmente para cumplir estrictamente el rúbrico sin alterar la identidad."
            )

        prompt = (
            "Edita la imagen para cumplir con las reglas de fotografía tipo documento. "
            "Aplica SOLO los cambios necesarios manteniendo la identidad. Cambios requeridos: "
            + "; ".join(fixes)
            + ". Exporta con calidad fotográfica, sin texto sobreimpreso."
        )
        return prompt, fixes

    async def agent_plan_from_notes(
        self, project_endpoint: Optional[str], image_name: str, notes: str
    ) -> Optional[str]:
        if not (
            self.AIProjectClient
            and self.DefaultAzureCredential
            and self.MessageInputTextBlock
            and project_endpoint
        ):
            return None

        model_name = os.getenv("MODEL_DEPLOYMENT_NAME")
        if not model_name:
            self.logger.warning("MODEL_DEPLOYMENT_NAME not configured; skipping agent planning")
            return None

        instructions = (
            "Eres un asistente que transforma 'notas de evaluación' en instrucciones de edición para un modelo de edición de imágenes (gpt-image-1). "
            "Devuelve SOLO un texto breve en español, en tono imperativo, con cambios concretos y mínimos para cumplir el rúbrico. "
            "Mantén la identidad de la persona. Evita lenguaje superfluo, sin JSON, sin explicaciones."
        )

        user_text = (
            f"Imagen: {image_name}\n"
            f"Notas de evaluación (extracto): {notes}\n\n"
            "Genera un texto de edición breve y accionable para pasar al modelo gpt-image-1 (endpoint de edits)."
        )

        missing: List[str] = []
        if self.DefaultAzureCredential is None:
            missing.append("DefaultAzureCredential")
        if self.AIProjectClient is None:
            missing.append("AIProjectClient")
        if self.MessageInputTextBlock is None:
            missing.append("MessageInputTextBlock")
        if missing:
            raise RuntimeError(
                "Azure AI SDK components are required for agent planning: "
                + ", ".join(missing)
            )

        async with self.DefaultAzureCredential() as credential:  # type: ignore[call-arg]
            async with self.AIProjectClient(credential=credential, endpoint=project_endpoint) as client:  # type: ignore[call-arg]
                agents = client.agents
                agent = await agents.create_agent(
                    model=model_name,
                    name="image-improve-planner",
                    instructions=instructions,
                )
                try:
                    thread = await agents.threads.create()
                    await agents.messages.create(  # type: ignore[attr-defined]
                        role="user",
                        thread_id=thread.id,
                        content=[self.MessageInputTextBlock(text=user_text)],
                    )
                    await agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)

                    last_text: Optional[str] = None
                    async for msg in agents.messages.list(thread_id=thread.id):
                        role_value = str(getattr(msg, "role", "")).lower()
                        if "agent" in role_value and msg.content:
                            content = msg.content[0]
                            if isinstance(content, dict) and content.get("text"):
                                text_obj = content.get("text")
                                value = (
                                    getattr(text_obj, "value", None)
                                    if hasattr(text_obj, "value")
                                    else text_obj.get("value")
                                )
                                if value:
                                    last_text = value
                    return last_text.strip() if last_text else None
                finally:
                    try:
                        await agents.delete_agent(agent.id)
                    except Exception:  # pragma: no cover - best effort cleanup
                        pass

    def ensure_project_and_deployment(
        self, project_endpoint: Optional[str], api_version: Optional[str] = None
    ) -> Tuple[str, str, str]:
        endpoint = (project_endpoint or os.getenv("PROJECT_ENDPOINT") or "").rstrip("/")
        if not endpoint:
            raise RuntimeError("PROJECT_ENDPOINT is required.")
        deployment = os.getenv("IMAGE_DEPLOYMENT_NAME")
        if not deployment:
            raise RuntimeError("IMAGE_DEPLOYMENT_NAME is required.")
        api_ver = api_version or "2025-04-01-preview"
        return endpoint, deployment, api_ver

    async def images_edits_via_project_async(
        self,
        project_endpoint: str,
        deployment: str,
        image_path: pathlib.Path,
        prompt: str,
        *,
        size: str = "1024x1024",
        api_version: Optional[str] = None,
    ) -> bytes:
        if self.DefaultAzureCredential is None or self.AIProjectClient is None:
            raise RuntimeError("Azure credentials or AI Project client are not available.")

        api_version = api_version or "2025-04-01-preview"

        try:
            from openai import AsyncAzureOpenAI  # noqa: F401  # pylint: disable=unused-import
        except Exception as exc:  # pragma: no cover - import failure is rare but fatal
            raise RuntimeError("The 'openai' package is required for image edits.") from exc

        image_input = image_path.read_bytes()
        image_buffer = io.BytesIO(image_input)
        image_buffer.name = image_path.name
        image_buffer.seek(0)

        size_literal = cast(Literal["256x256", "512x512", "1024x1024"], size)

        async with self.DefaultAzureCredential() as credential:  # type: ignore[call-arg]
            async with self.AIProjectClient(credential=credential, endpoint=project_endpoint) as client:  # type: ignore[call-arg]
                openai_client = await client.get_openai_client(api_version=api_version)
                response = await openai_client.images.edit(
                    model=deployment,
                    image=image_buffer,
                    prompt=prompt,
                    size=size_literal,
                    n=1,
                )

        data = getattr(response, "data", None)
        if not data:
            raise RuntimeError("Image edit response did not include any data")

        first = data[0]
        encoded: Optional[str] = None
        url: Optional[str] = None

        if hasattr(first, "b64_json"):
            encoded = getattr(first, "b64_json")
        elif isinstance(first, dict):
            encoded = first.get("b64_json")

        if not encoded:
            if hasattr(first, "url"):
                url = getattr(first, "url")
            elif isinstance(first, dict):
                url = first.get("url")

        if encoded:
            return base64.b64decode(encoded)

        if url:
            if requests is None:
                raise RuntimeError("The 'requests' package is required to download generated images.")
            resp = requests.get(url, timeout=180)
            resp.raise_for_status()
            return resp.content

        raise RuntimeError("Image edit response did not include image bytes")

    def split_fix_candidates(self, prompt: str) -> List[str]:
        return [segment.strip() for segment in prompt.split(";") if segment.strip()]

    async def resolve_prompt(
        self, job: "ImageImprovementJob", project_endpoint: str, image_name: str
    ) -> Tuple[str, List[str]]:
        if job.prompt_override:
            prompt = job.prompt_override.strip()
            return prompt, self.split_fix_candidates(prompt)

        if job.notes:
            try:
                agent_prompt = await self.agent_plan_from_notes(
                    project_endpoint, image_name, job.notes
                )
            except Exception:
                agent_prompt = None
            if agent_prompt:
                return agent_prompt, self.split_fix_candidates(agent_prompt)

        eval_item: Dict[str, Any] = {
            "criteria_scores": job.criteria_scores,
            "notes": job.notes,
        }
        return self.derive_improvement_instructions(eval_item)

    def get_auth_headers(self) -> Dict[str, str]:
        """Return authorization headers if a token is configured."""

        if not self._auth_token:
            return {}
        return {"Authorization": f"Bearer {self._auth_token}"}


def get_improvement_hook() -> ImprovementHook:
    """Public accessor for the singleton hook."""

    return ImprovementHook()


__all__ = ["ImprovementHook", "get_improvement_hook"]
