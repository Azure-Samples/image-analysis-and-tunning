"""API-facing Pydantic models shared by the analysis service."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ApiError(BaseModel):
    """Standard envelope for errors returned by the HTTP layer."""

    code: str = Field(..., description="Stable, machine-readable error code")
    message: str = Field(..., description="Human-friendly error description")
    details: Optional[Any] = Field(
        default=None,
        description="Optional raw details that may help debugging the failure",
    )
    action: str = Field(
        default="",
        description="Recommended follow-up action the caller can take to recover",
    )


class ErrorResponse(BaseModel):
    """Wrapper used to document error responses in the OpenAPI schema."""

    error: ApiError


class HealthResponse(BaseModel):
    """Response body for health checks."""

    status: str = Field(..., description="Overall service status string")
    detail: Optional[str] = Field(
        default=None,
        description="Additional diagnostic information when status != 'ok'",
    )
