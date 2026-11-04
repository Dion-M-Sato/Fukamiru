"""Versioned node input/output contracts shared by every workflow."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class NodeStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_type: str
    artifact_id: str
    revision_id: str | None = None
    content_sha256: str | None = None


class ClaimRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    relation: Literal["input", "created", "updated", "invalidated"] = "input"


class DefectReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defect_type: str
    severity: Literal["info", "minor", "major", "critical"]
    message: str
    object_type: str | None = None
    object_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class MetricValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: float | str
    unit: str | None = None


class HumanReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_type: str
    object_id: str
    question: str
    options: list[str] = Field(default_factory=list)
    blocking: bool = True


class NodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1"] = "1"
    run_id: str
    node_id: str
    attempt: int = Field(default=1, ge=1)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    claims: list[ClaimRef] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class NodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1"] = "1"
    status: NodeStatus
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    claims: list[ClaimRef] = Field(default_factory=list)
    defects: list[DefectReport] = Field(default_factory=list)
    metrics: list[MetricValue] = Field(default_factory=list)
    review_requests: list[HumanReviewRequest] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
