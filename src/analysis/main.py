"""FastAPI application exposing the image analysis capabilities."""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .analysis import evaluate_image
from .api_models import ErrorResponse, HealthResponse
from .schemas import ImageEvaluationRequest, ImageEvaluationResponse
from .utils import get_analysis_hook

APP_DESCRIPTION = (
    "Service that scores document-style portrait photos using Azure AI Agents. "
    "It accepts an image and returns structured rubric scores that downstream "
    "systems can consume."
)
HOOK = get_analysis_hook()


app = FastAPI(
    title="Image Analysis API",
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
    """Simple liveness probe used by orchestrators."""

    return HealthResponse(status="ok", detail="ready")


@app.post(
    "/v1/evaluations",
    response_model=ImageEvaluationResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request payload"},
        413: {"model": ErrorResponse, "description": "Payload too large"},
        422: {"description": "Validation error"},
        502: {"model": ErrorResponse, "description": "Evaluation agent failure"},
        500: {"model": ErrorResponse, "description": "Unexpected server error"},
    },
    tags=["evaluations"],
)
async def evaluate_endpoint(
    image: UploadFile = File(..., description="Image to evaluate"),
    prompt: str = Form(
        ..., description="Instructions passed to the agent along with the rubric"
    ),
    model_deployment_name: Optional[str] = Form(
        default=None,
        description="Azure AI deployment name to use; defaults to MODEL_DEPLOYMENT_NAME",
    ),
    project_endpoint: Optional[str] = Form(
        default=None,
        description="Azure AI project endpoint; defaults to PROJECT_ENDPOINT",
    ),
) -> ImageEvaluationResponse:
    """Score an uploaded image following the strict rubric."""

    if not image.filename:
        raise HOOK.build_error_exception(
            400,
            code="missing_filename",
            message="Uploaded file must include a filename",
            action="Send a file with a descriptive filename",
        )

    temp_path = await HOOK.persist_upload_temporarily(image)
    request = ImageEvaluationRequest(
        image_path=temp_path,
        prompt=prompt,
        model_deployment_name=model_deployment_name,
        project_endpoint=project_endpoint,
    )

    try:
        response = await evaluate_image(request)
    except HTTPException:
        HOOK.cleanup_temp_file(temp_path)
        raise
    except Exception as exc:  # pragma: no cover - defensive safety net
        HOOK.cleanup_temp_file(temp_path)
        raise HOOK.build_error_exception(
            500,
            code="evaluation_unexpected_error",
            message="Unexpected error while evaluating the image",
            details=str(exc),
            action="Inspect server logs or retry later",
        ) from exc

    HOOK.cleanup_temp_file(temp_path)

    if not response.success:
        raise HOOK.build_error_exception(
            502,
            code="evaluation_failed",
            message=response.error or "The evaluation agent did not return a result",
            details=response.error,
            action="Verify Azure credentials and that the image is accessible",
        )

    return response
