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
from typing import Optional

from azure.core.exceptions import HttpResponseError
from azure.identity.aio import DefaultAzureCredential
from azure.ai.projects.aio import AIProjectClient

from azure.ai.agents.models import (
    MessageInputTextBlock,
    MessageImageFileParam,
    MessageInputImageFileBlock,
)

from image_analysis.schemas import (
    ImageEvaluationRequest,
    ImageEvaluationResponse,
    ImageEvaluationResult,
)


RUBRIC_INSTRUCTIONS = (
    "You are an image evaluation assistant. Follow this strict rubric and always "
    "return only a single JSON object (no additional text) with the keys: "
    "overall_score (integer 0-100), criteria_scores (object mapping str->int), "
    "safe (boolean), and notes (string).\n\n"

    "Scoring rubric (for consistent, repeatable validations):\n"
    "- composition: 0-25\n"
    "- exposure: 0-20\n"
    "- sharpness: 0-15\n"
    "- relevance_to_prompt: 0-30\n"
    "- safety: 0-10\n\n"

    "Compute overall_score as the sum of the criteria above (clamp to 0-100). "
    "Be concise in `notes` and explain the most important reasons for the score. "
    "If you cannot score an image for any reason, return overall_score=0, "
    "safe=false and a short note explaining why."
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
    created_agent = False
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
                created_agent = True

            thread = await agents_client.threads.create()
            image_file = await agents_client.files.upload_and_poll(
                file_path=request.image_path,
                purpose="assistants"
            )
            file_param = MessageImageFileParam(file_id=image_file.id, detail="high")

            user_text = (
                request.prompt
                + "\n\nStrict output format required: return ONLY a JSON object with the keys 'overall_score', 'criteria_scores', 'safe', and 'notes'."
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
                    response_format="json_object",
                )
            except TypeError:
                run = await agents_client.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)

            agent_text = None
            async for msg in agents_client.messages.list(thread_id=thread.id):
                role_value = str(getattr(msg, "role", "")).lower()
                if role_value == "agent" and getattr(msg, "text_messages", None):
                    last_text = msg.text_messages[-1]
                    agent_text = last_text.text.value

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
            if agents_client is not None and created_agent and agent is not None:
                await agents_client.delete_agent(agent.id)
        except Exception:
            pass

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
    import asyncio
    return asyncio.run(evaluate_image(req))


def _is_image_file(path: str) -> bool:
    """Return True if file extension looks like an image."""
    exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}
    _, ext = os.path.splitext(path.lower())
    return ext in exts


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

    images = [p for p in assets_dir.iterdir() if p.is_file() and _is_image_file(p.name)]
    if not images:
        print(f"No images found in {assets_dir}")
        return 0

    failures = 0
    for img in images:
        resp = evaluate_image_simple(str(img), args.prompt)
        if resp.success and resp.result:
            r = resp.result
            notes_preview = (r.notes or "")[:120]
            print(f"{img.name}: score={r.overall_score}, safe={r.safe}, notes={notes_preview}")
        else:
            failures += 1
            print(f"{img.name}: ERROR: {resp.error}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
