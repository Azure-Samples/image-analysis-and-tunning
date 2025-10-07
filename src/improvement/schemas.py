"""Pydantic models for the image improvement service."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ImageImprovementJob(BaseModel):
    """Input payload describing an improvement job."""

    image_path: str = Field(..., description="Absolute path to the image on disk")
    notes: Optional[str] = Field(
        default=None,
        description="Optional evaluation notes that highlight the issues to fix",
    )
    criteria_scores: Dict[str, int] = Field(
        default_factory=dict,
        description="Optional rubric scores used to derive automatic fixes",
    )
    project_endpoint: Optional[str] = Field(
        default=None,
        description="Azure AI project endpoint; defaults to PROJECT_ENDPOINT env var",
    )
    size: Literal["256x256", "512x512", "1024x1024"] = Field(
        default="1024x1024",
        description="Target output size supported by the image edit model",
    )
    api_version: Optional[str] = Field(
        default=None,
        description="Azure OpenAI API version; defaults to SDK fallback",
    )
    prompt_override: Optional[str] = Field(
        default=None,
        description="Optional explicit prompt to send to the image edit model",
    )


class ImageImprovementResult(BaseModel):
    """Successful improvement output."""

    filename: str = Field(..., description="Filename associated with the improved image")
    content_type: str = Field(..., description="MIME type of the improved image")
    image_b64: str = Field(..., description="Improved image encoded as base64 string")
    prompt: str = Field(..., description="Prompt that guided the edit")
    applied_fixes: List[str] = Field(
        default_factory=list,
        description="List of concrete fixes the service attempted to apply",
    )


class ImageImprovementResponse(BaseModel):
    """Top-level response envelope for improvement calls."""

    success: bool = Field(..., description="Whether the improvement succeeded")
    result: Optional[ImageImprovementResult] = Field(
        default=None, description="Improvement payload on success"
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message on failure, intended for human debugging",
    )
    details: Optional[Any] = Field(
        default=None,
        description="Optional machine-readable failure details",
    )
