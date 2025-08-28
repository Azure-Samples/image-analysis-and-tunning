"""
Pydantic schemas for the image-evaluation function interfaces.
"""
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class ImageEvaluationRequest(BaseModel):
    """Request schema for evaluating an image.

    Attributes:
        image_path: Local path to the image file to evaluate.
        prompt: A short instruction describing what to evaluate in the image.
        model_deployment_name: Optional. If provided it overrides the env var
            MODEL_DEPLOYMENT_NAME used to pick the model to run the agent.
        project_endpoint: Optional. If provided it overrides the env var
            PROJECT_ENDPOINT used to locate the AI Foundry project.
    """

    image_path: str = Field(..., description="Local path to the image file")
    prompt: str = Field(..., description="Text prompt describing the evaluation task")
    model_deployment_name: Optional[str] = Field(
        None, description="Optional model deployment name; falls back to env var MODEL_DEPLOYMENT_NAME"
    )
    project_endpoint: Optional[str] = Field(
        None, description="Optional AI Foundry project endpoint; falls back to env var PROJECT_ENDPOINT"
    )


class ImageEvaluationResult(BaseModel):
    """Structured evaluation result returned by the agent.

    Attributes:
        overall_score: Integer overall score (0-100).
        criteria_scores: Mapping of named criteria to integer scores.
        safe: Whether the image is considered safe.
        notes: Short textual explanation or notes from the agent.
        raw: Raw JSON payload returned by the agent (for diagnostics).
        agent_id: The agent id used to perform the evaluation (if available).
        thread_id: The thread id used for the conversation (if available).
        run_status: Final run status returned by the Agents run.
    """

    overall_score: int = Field(..., ge=0, le=100)
    criteria_scores: Dict[str, int] = Field(default_factory=dict)
    safe: bool = Field(...)
    notes: Optional[str] = Field(None)
    raw: Optional[Dict[str, Any]] = Field(None)
    agent_id: Optional[str] = Field(None)
    thread_id: Optional[str] = Field(None)
    run_status: Optional[str] = Field(None)


class ImageEvaluationResponse(BaseModel):
    """Top-level response for the evaluation function.

    Attributes:
        success: Whether the evaluation succeeded.
        result: The parsed, structured evaluation result (when success is True).
        error: Error message (when success is False).
    """

    success: bool = Field(...)
    result: Optional[ImageEvaluationResult] = Field(None)
    error: Optional[str] = Field(None)
