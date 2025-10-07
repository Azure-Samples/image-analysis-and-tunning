"""FastAPI application exposing the image improvement capabilities."""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from analysis.api_models import ErrorResponse, HealthResponse
from .improvement import ImageImprovementJob, improve_image
from .schemas import ImageImprovementResponse
from .utils import get_improvement_hook


APP_DESCRIPTION = (
    "Service that enhances document-style photos using Azure AI image edits. "
    "It accepts a problematic photo plus optional evaluation metadata and "
    "returns an improved image alongside the applied fixes."
)


HOOK = get_improvement_hook()


app = FastAPI(
    title="Image Improvement API",
    description=APP_DESCRIPTION,
    version=os.getenv("APP_VERSION", "0.1.0"),
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)

origins = HOOK.parse_csv_env("CORS_ALLOW_ORIGINS", ["*"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_model=HealthResponse, tags=["health"])
async def healthcheck() -> HealthResponse:
    return HealthResponse(status="ok", detail="ready")


@app.post(
    "/v1/improvements",
    response_model=ImageImprovementResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request payload"},
        413: {"model": ErrorResponse, "description": "Payload too large"},
        422: {"description": "Validation error"},
        502: {"model": ErrorResponse, "description": "Image edit agent failure"},
        500: {"model": ErrorResponse, "description": "Unexpected server error"},
    },
    tags=["improvements"],
)
async def improve_endpoint(
    image: UploadFile = File(..., description="Image to improve"),
    notes: Optional[str] = Form(
        default=None,
        description="Optional evaluation notes that describe the issues to fix",
    ),
    criteria_scores: Optional[str] = Form(
        default=None,
        description="JSON object with rubric scores (e.g. {\"fondo_blanco\": 10})",
    ),
    project_endpoint: Optional[str] = Form(
        default=None,
        description="Azure AI project endpoint; defaults to PROJECT_ENDPOINT",
    ),
    size: str = Form(
        default="1024x1024",
        description="Desired output size (256x256, 512x512 or 1024x1024)",
    ),
    api_version: Optional[str] = Form(
        default=None,
        description="Optional Azure OpenAI API version override",
    ),
    prompt_override: Optional[str] = Form(
        default=None,
        description="Explicit prompt to send to the image editor (bypasses heuristics)",
    ),
) -> ImageImprovementResponse:
    if not image.filename:
        raise HOOK.build_error_exception(
            400,
            code="missing_filename",
            message="Uploaded file must include a filename",
            action="Send a file with a descriptive filename",
        )

    parsed_scores = HOOK.parse_criteria_scores(criteria_scores)
    size = HOOK.validate_output_size(size)
    temp_path = await HOOK.persist_upload_temporarily(image)
    job = ImageImprovementJob(
        image_path=temp_path,
        notes=notes,
        criteria_scores=parsed_scores,
        project_endpoint=project_endpoint,
        size=size,  # type: ignore[arg-type]
        api_version=api_version,
        prompt_override=prompt_override,
    )

    try:
        response = await improve_image(job)
    except HTTPException:
        HOOK.cleanup_temp_file(temp_path)
        raise
    except Exception as exc:  # pragma: no cover - defensive safety net
        HOOK.cleanup_temp_file(temp_path)
        raise HOOK.build_error_exception(
            500,
            code="improvement_unexpected_error",
            message="Unexpected error while improving the image",
            details=str(exc),
            action="Inspect server logs or retry later",
        ) from exc

    HOOK.cleanup_temp_file(temp_path)

    if not response.success or not response.result:
        raise HOOK.build_error_exception(
            502,
            code="improvement_failed",
            message=response.error or "The image edit agent did not return a result",
            details=response.details or response.error,
            action="Verify Azure credentials and that the source image is valid",
        )

    return response
