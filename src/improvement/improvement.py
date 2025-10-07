#!/usr/bin/env python3
"""Image improvement helpers using Azure AI Projects + Azure OpenAI image edits."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

from .schemas import ImageImprovementJob, ImageImprovementResponse, ImageImprovementResult
from .utils import get_improvement_hook

# No sync credentials needed; we use the async DefaultAzureCredential within AIProjectClient context

SRC_DIR = Path(__file__).resolve().parents[1]

ASSETS_DIR = SRC_DIR / "analysis" / ".assets"
IMPROVED_DIR = ASSETS_DIR / "improved"

HOOK = get_improvement_hook()


async def improve_image(job: ImageImprovementJob) -> ImageImprovementResponse:
    """High-level async API: returns the improved image bytes and applied fixes."""

    image_path = Path(job.image_path)
    if not image_path.exists():
        return ImageImprovementResponse(
            success=False,
            error=f"Image not found: {image_path}",
            details={"path": str(image_path)},
        )

    try:
        project_endpoint, deployment, _ = HOOK.ensure_project_and_deployment(
            job.project_endpoint, job.api_version
        )
        prompt, fixes = await HOOK.resolve_prompt(job, project_endpoint, image_path.name)
        image_bytes = await HOOK.images_edits_via_project_async(
            project_endpoint,
            deployment,
            image_path,
            prompt,
            size=job.size,
        )
        result = ImageImprovementResult(
            filename=image_path.name,
            content_type=HOOK.guess_mime(image_path),
            image_bytes=image_bytes,
            prompt=prompt,
            applied_fixes=fixes,
        )
        return ImageImprovementResponse(success=True, result=result)
    except Exception as exc:
        return ImageImprovementResponse(
            success=False,
            error=str(exc),
            details={"exception_type": exc.__class__.__name__},
        )

async def _run_cli_job(args: argparse.Namespace) -> int:
    assets_dir = Path(args.assets_dir)
    evaluations = HOOK.load_evaluations(assets_dir / "evaluations.json")
    if not evaluations:
        print(f"No evaluations found in {assets_dir / 'evaluations.json'}")
        return 0

    IMPROVED_DIR.mkdir(parents=True, exist_ok=True)

    improved_summary: List[Dict[str, object]] = []
    for evaluation in evaluations:
        if not isinstance(evaluation, dict):
            continue
        filename = evaluation.get("filename")
        if not filename:
            continue
        image_path = assets_dir / filename
        if not image_path.exists():
            print(f"Image not found for evaluation entry: {filename}", file=sys.stderr)
            improved_summary.append({"filename": filename, "error": "image not found"})
            continue

        job = ImageImprovementJob(
            image_path=str(image_path),
            notes=str(evaluation.get("notes") or ""),
            criteria_scores=evaluation.get("criteria_scores") or {},  # type: ignore[arg-type]
            project_endpoint=args.project_endpoint,
            size=args.size,
            api_version=args.api_version,
        )

        response = await improve_image(job)
        if response.success and response.result:
            out_path = IMPROVED_DIR / image_path.name
            out_path.write_bytes(response.result.image_bytes)
            print(
                f"Improved: {image_path.name} -> {out_path.name} | Fixes: {', '.join(response.result.applied_fixes)}"
            )
            improved_summary.append(
                {
                    "filename": image_path.name,
                    "output_path": str(out_path.relative_to(assets_dir)),
                    "applied_fixes": response.result.applied_fixes,
                }
            )
        else:
            print(
                f"Failed to improve {image_path.name}: {response.error}", file=sys.stderr
            )
            improved_summary.append(
                {
                    "filename": image_path.name,
                    "error": response.error,
                }
            )

    if args.summary:
        summary_path = assets_dir / "improvements_summary.json"
        try:
            with summary_path.open("w", encoding="utf-8") as fh:
                json.dump(improved_summary, fh, ensure_ascii=False, indent=2)
            print(f"Wrote summary to {summary_path}")
        except Exception as exc:
            print(f"Failed to write summary: {exc}", file=sys.stderr)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Improve images using Azure AI Projects + Azure OpenAI Images Edits"
    )
    parser.add_argument(
        "--assets-dir",
        default=str(ASSETS_DIR),
        help="Path to analysis/.assets directory",
    )
    parser.add_argument(
        "--project-endpoint",
        default=os.getenv("PROJECT_ENDPOINT"),
        help="Azure AI Foundry Project endpoint",
    )
    parser.add_argument(
        "--size",
        default="1024x1024",
        choices=["256x256", "512x512", "1024x1024"],
        help="Output size",
    )
    parser.add_argument(
        "--api-version",
        default="2024-12-01-preview",
        help="API version for Images Edits endpoint",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Write improvements_summary.json under assets dir",
    )
    args = parser.parse_args()
    return asyncio.run(_run_cli_job(args))


if __name__ == "__main__":
    raise SystemExit(main())
