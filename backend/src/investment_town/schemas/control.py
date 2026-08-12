from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

ProjectState = Literal["idle", "running", "paused", "stopped", "failed", "killed"]
ProjectHealth = Literal["healthy", "degraded", "halted"]


class Project(BaseModel):
    project_id: str
    name: str
    state: ProjectState
    health: ProjectHealth
    updated_at: datetime


class CommandRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ControlEvent(BaseModel):
    sequence: int
    event_id: UUID
    event_type: str
    project_id: str
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuditEntry(BaseModel):
    audit_id: UUID
    request_id: UUID
    project_id: str
    actor: str
    action: str
    reason: str | None
    from_state: ProjectState
    to_state: ProjectState
    created_at: datetime


class CommandResult(BaseModel):
    project: Project
    event: ControlEvent
    audit: AuditEntry
