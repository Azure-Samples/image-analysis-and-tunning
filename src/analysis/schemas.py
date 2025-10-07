"""Pydantic schemas for the image-evaluation function interfaces."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ImageEvaluationRequest(BaseModel):
	"""Request schema for evaluating an image."""

	image_path: str = Field(..., description="Local path to the image file")
	prompt: str = Field(..., description="Text prompt describing the evaluation task")
	model_deployment_name: Optional[str] = Field(
		None,
		description="Optional model deployment name; falls back to env var MODEL_DEPLOYMENT_NAME",
	)
	project_endpoint: Optional[str] = Field(
		None,
		description="Optional AI Foundry project endpoint; falls back to env var PROJECT_ENDPOINT",
	)


class ImageEvaluationResult(BaseModel):
	"""Structured evaluation result returned by the agent."""

	overall_score: int = Field(..., ge=0, le=100)
	criteria_scores: Dict[str, int] = Field(default_factory=dict)
	safe: bool = Field(...)
	notes: Optional[str] = Field(None)
	raw: Optional[Dict[str, Any]] = Field(None)
	agent_id: Optional[str] = Field(None)
	thread_id: Optional[str] = Field(None)
	run_status: Optional[str] = Field(None)


class ImageEvaluationResponse(BaseModel):
	"""Top-level response for the evaluation function."""

	success: bool = Field(...)
	result: Optional[ImageEvaluationResult] = Field(None)
	error: Optional[str] = Field(None)


__all__ = [
	"ImageEvaluationRequest",
	"ImageEvaluationResult",
	"ImageEvaluationResponse",
]
