from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

ResearchRunStatus = Literal["running", "paused", "completed", "failed"]
ResearchTaskStatus = Literal["queued", "completed", "skipped", "failed"]
CheckpointState = Literal["completed", "paused"]


class ResearchRun(BaseModel):
    run_id: UUID
    project_id: str
    ticker: str
    analysis_date: date
    status: ResearchRunStatus
    current_stage: int
    attempt: int = 1
    final_rating: str | None = None
    proposal_id: UUID | None = None
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost: Decimal = Decimal(0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None


class ResearchAgentTask(BaseModel):
    task_id: UUID
    run_id: UUID
    agent_id: str
    stage: int
    status: ResearchTaskStatus
    summary: str
    attempt: int = 1
    confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class BlackboardEntry(BaseModel):
    entry_id: UUID
    run_id: UUID
    agent_id: str
    topic: str
    content: str
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchCheckpoint(BaseModel):
    checkpoint_id: UUID
    run_id: UUID
    stage: int
    state: CheckpointState
    completed_agents: list[str]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ModelUsage(BaseModel):
    usage_id: UUID
    run_id: UUID
    agent_id: str
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost: Decimal = Decimal(0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchRunDetail(BaseModel):
    run: ResearchRun
    tasks: list[ResearchAgentTask]
    blackboard: list[BlackboardEntry]
    checkpoints: list[ResearchCheckpoint] = Field(default_factory=list)
    usage: list[ModelUsage] = Field(default_factory=list)
