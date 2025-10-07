"""Image evaluation helpers using Azure AI Agents (AI Foundry).

This module implements a single entrypoint `evaluate_image` which accepts a
pydantic `ImageEvaluationRequest` and returns `ImageEvaluationResponse`.

The implementation follows the patterns in the `azure-ai-projects` /
`azure-ai-agents` SDKs: create an agent with a strict rubric instruction,
upload the image as an assistant file, create a message containing the image
and the user prompt, run the agent, and parse the JSON object the agent
must return.

Note: The function requires Azure authentication (DefaultAzureCredential) and
an AI Foundry project endpoint in the environment variable `PROJECT_ENDPOINT`
(or supplied in the request). It also requires a deployed model name in
`MODEL_DEPLOYMENT_NAME` (or supplied in the request).
"""
from __future__ import annotations

import os
import json
import asyncio

from typing import Optional

from dotenv import load_dotenv

from azure.core.exceptions import HttpResponseError
from azure.identity.aio import DefaultAzureCredential
from azure.ai.projects.aio import AIProjectClient

from azure.ai.agents.models import (
    MessageInputTextBlock,
    MessageImageFileParam,
    MessageInputImageFileBlock
)

from .schemas import ImageEvaluationRequest, ImageEvaluationResponse, ImageEvaluationResult
from .utils import get_analysis_hook

load_dotenv()

HOOK = get_analysis_hook()


RUBRIC_INSTRUCTIONS = (
    "Eres un asistente de evaluación de fotografías tipo documento. Sigue este rúbrico estricto y devuelve SIEMPRE "
    "un único objeto JSON (sin texto adicional) con las claves: "
    "overall_score (entero 0-100), criteria_scores (objeto que mapea str->int), "
    "safe (booleano) y notes (cadena en español).\n\n"

    "Reglas a validar y puntuación (para validaciones consistentes y repetibles):\n"
    "- tamaño_3x4: 0-25 — La imagen debe tener proporción 3:4 (ancho:alto ≈ 3:4, tolerancia ±5%). La imagen debe tener las axilas y los pelos proximos de la parte de arriba y abajo de la imagen.\n"
    "- fondo_blanco: 0-25 — El fondo debe ser blanco o muy cercano a blanco, uniforme y sin patrones.\n"
    "- mirada_frontal_rostro_homogeneo: 0-20 — La persona debe mirar al frente, cabeza centrada, rostro totalmente visible y con iluminación homogénea.\n"
    "- sin_dientes_visibles: 0-10 — La persona no debe mostrar los dientes (labios relajados y cerrados).\n"
    "- identificable_sin_obstrucciones: 0-20 — Nada debe impedir la identificación (sin mascarillas, gafas de sol, viseras, objetos, sombras fuertes ni filtros; gafas transparentes aceptables si no tapan los ojos).\n\n"

    "Calcula overall_score como la suma de los criterios anteriores (limita a 0-100). "
    "Establece safe=true solo si TODAS las reglas están cumplidas; en caso contrario, safe=false.\n\n"

    "Formato de notes (en español y conciso):\n"
    "- Si hay incumplimientos, lista cada regla NO respetada y explica por qué no se cumple (máximo 2 líneas por punto).\n"
    "- Si todas se cumplen, indica brevemente que la foto cumple con los requisitos.\n\n"

    "Si no puedes puntuar la imagen por cualquier motivo, devuelve overall_score=0, safe=false y una nota corta explicando el motivo (en español)."
)

async def evaluate_image(request: ImageEvaluationRequest) -> ImageEvaluationResponse:
    """Evaluate a local image using an Azure AI Foundry Agent.

    This function will:
    - Create an ephemeral agent whose instructions contain a deterministic
      evaluation rubric.
    - Upload the provided image to the project for assistant use.
    - Send a message that includes the image and the user's prompt.
    - Run the agent (synchronously, polling on the service) and parse the
      JSON object the agent must return.
    - Clean up created resources when possible.

    Args:
        request: ImageEvaluationRequest containing `image_path` and `prompt` as
            well as optional `model_deployment_name` and `project_endpoint`.

    Returns:
        ImageEvaluationResponse: success + structured result or error.

    Raises:
        No exceptions are propagated; errors are returned inside the response
        to keep the interface simple for callers.
    """

    model_name = request.model_deployment_name or os.getenv("MODEL_DEPLOYMENT_NAME")
    endpoint = request.project_endpoint or os.getenv("PROJECT_ENDPOINT")

    if not endpoint:
        return ImageEvaluationResponse(
            success=False,
            result=None,
            error="PROJECT_ENDPOINT not set and not provided in request"
        )

    if not model_name:
        return ImageEvaluationResponse(
            success=False,
            result=None,
            error="MODEL_DEPLOYMENT_NAME not set and not provided in request"
        )

    credential = DefaultAzureCredential()
    project_client = AIProjectClient(credential=credential, endpoint=endpoint)

    agent = None
    image_file = None
    agents_client = None

    try:
        async with project_client:
            agents_client = project_client.agents

            agent_name = os.getenv("AGENT_NAME", "image-evaluator")
            agent_id_from_env = os.getenv("AGENT_ID", None)

            if agent_id_from_env:
                try:
                    agent = await agents_client.get_agent(agent_id_from_env)
                except Exception:
                    agent = None

            if agent is None:
                agent = await agents_client.create_agent(
                    model=model_name,
                    name=agent_name,
                    instructions=RUBRIC_INSTRUCTIONS,
                )

            thread = await agents_client.threads.create()
            image_file = await agents_client.files.upload_and_poll(
                file_path=request.image_path,
                purpose="assistants"
            )
            file_param = MessageImageFileParam(file_id=image_file.id, detail="high")

            user_text = (
                request.prompt
                + "\n\nFormato de salida estricto: devuelve SOLO un objeto JSON con las claves 'overall_score', 'criteria_scores', 'safe' y 'notes'. "
                + "La nota ('notes') debe estar en español. Si hay incumplimientos, lista cuáles características NO fueron respetadas y por qué."
            )

            content_blocks = [
                MessageInputTextBlock(text=user_text),
                MessageInputImageFileBlock(image_file=file_param),
            ]

            _ = await agents_client.messages.create(
                role="user",
                thread_id=thread.id,
                content=content_blocks
            )

            try:
                run = await agents_client.runs.create_and_process(
                    thread_id=thread.id,
                    agent_id=agent.id,
                    response_format="auto",
                )
            except TypeError:
                run = await agents_client.runs.create_and_process(
                    thread_id=thread.id,
                    agent_id=agent.id
                )

            agent_text = None
            async for msg in agents_client.messages.list(thread_id=thread.id):
                role_value = str(getattr(msg, "role", "")).lower()
                content = msg.content[0]
                if "agent" in role_value and content.get("text", None):
                    last_text = content
                    agent_text = last_text.get('text', {}).value

        if not agent_text:
            return ImageEvaluationResponse(
                success=False,
                result=None,
                error="Agent did not return any text message. Raw run status: " + str(getattr(run, "status", None)),
            )

        try:
            parsed = json.loads(agent_text.strip())
        except Exception:
            return ImageEvaluationResponse(success=False, result=None, error="Agent response was not valid JSON")

        try:
            overall_score = int(parsed.get("overall_score", 0))
        except Exception:
            overall_score = 0

        criteria_scores = parsed.get("criteria_scores") or {}
        safe = bool(parsed.get("safe", False))
        notes = parsed.get("notes") or parsed.get("explanation") or ""

        result = ImageEvaluationResult(
            overall_score=max(0, min(100, overall_score)),
            criteria_scores={k: int(v) for k, v in criteria_scores.items()} if isinstance(criteria_scores, dict) else {},
            safe=safe,
            notes=notes,
            raw={"agent_text": agent_text, "parsed": parsed},
            agent_id=getattr(agent, "id", None),
            thread_id=thread.id,
            run_status=getattr(run, "status", None),
        )

        return ImageEvaluationResponse(success=True, result=result, error=None)

    except HttpResponseError as e:
        return ImageEvaluationResponse(success=False, result=None, error=f"HTTP error from Azure SDK: {e.status_code} {e.message}")
    except Exception as e:
        return ImageEvaluationResponse(success=False, result=None, error=str(e))
    finally:
        try:
            if agents_client is not None and image_file is not None:
                await agents_client.files.delete(file_id=image_file.id)
        except Exception:
            pass


def evaluate_image_simple(image_path: str, prompt: str, model_deployment_name: Optional[str] = None, project_endpoint: Optional[str] = None) -> ImageEvaluationResponse:
    """Simple wrapper that constructs an ImageEvaluationRequest and calls evaluate_image.

    Args:
        image_path: Local path to the image file.
        prompt: Text describing what to evaluate.
        model_deployment_name: Optional model name; overrides environment variable.
        project_endpoint: Optional project endpoint; overrides environment variable.

    Returns:
        ImageEvaluationResponse
    """

    req = ImageEvaluationRequest(
        image_path=image_path,
        prompt=prompt,
        model_deployment_name=model_deployment_name,
        project_endpoint=project_endpoint,
    )
    # Run the async evaluator in a fresh event loop
    return asyncio.run(evaluate_image(req))


def main() -> int:
    """Batch-evaluate all images under the local `.assets` folder.

    Uses environment variables when present:
    - PROJECT_ENDPOINT, MODEL_DEPLOYMENT_NAME (required by SDK)
    - AGENT_ID (optional; reuse agent)

    CLI options:
    - --assets-dir: override the assets directory (defaults to sibling `.assets`).
    - --prompt: override the evaluation prompt (defaults to a generic prompt).
    """

    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Evaluate images with Azure AI Agents")
    parser.add_argument("--assets-dir", default=str(Path(__file__).parent / ".assets"))
    parser.add_argument(
        "--prompt",
        default=os.getenv(
            "EVAL_PROMPT",
            "Evaluate this image against the rubric and provide overall_score, criteria_scores, safe, and notes.",
        ),
    )
    args = parser.parse_args()

    assets_dir = Path(args.assets_dir)
    if not assets_dir.exists() or not assets_dir.is_dir():
        print(f"Assets directory not found: {assets_dir}")
        return 2

    images = [p for p in assets_dir.iterdir() if p.is_file() and HOOK.is_image_file(p.name)]
    if not images:
        print(f"No images found in {assets_dir}")
        return 0

    failures = 0
    evaluations = []
    for img in images:
        resp = evaluate_image_simple(str(img), args.prompt)
        if resp.success and resp.result:
            r = resp.result
            notes_preview = (r.notes or "")[:120]
            print(f"{img.name}: score={r.overall_score}, safe={r.safe}, notes={notes_preview}")
            evaluations.append({
                "filename": img.name,
                "success": True,
                "overall_score": r.overall_score,
                "criteria_scores": r.criteria_scores,
                "safe": r.safe,
                "notes": r.notes,
            })
        else:
            failures += 1
            print(f"{img.name}: ERROR: {resp.error}")
            evaluations.append({
                "filename": img.name,
                "success": False,
                "overall_score": None,
                "criteria_scores": {},
                "safe": None,
                "notes": resp.error,
            })

    # Persist all evaluations to evaluations.json under the assets directory
    try:
        out_path = assets_dir / "evaluations.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(evaluations, f, ensure_ascii=False, indent=2)
        print(f"Saved evaluations to {out_path}")
    except Exception as e:
        print(f"Failed to write evaluations.json: {e}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
