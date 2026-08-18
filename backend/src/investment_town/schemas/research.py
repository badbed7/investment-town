from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

ResearchRunStatus = Literal["running", "completed", "failed"]
ResearchTaskStatus = Literal["queued", "completed", "skipped", "failed"]


class ResearchRun(BaseModel):
    run_id: UUID
    project_id: str
    ticker: str
    analysis_date: date
    status: ResearchRunStatus
    current_stage: int
    final_rating: str | None = None
    proposal_id: UUID | None = None
    error: str | None = None
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


class ResearchRunDetail(BaseModel):
    run: ResearchRun
    tasks: list[ResearchAgentTask]
    blackboard: list[BlackboardEntry]

