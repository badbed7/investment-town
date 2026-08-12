from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

AgentStatus = Literal["idle", "working", "waiting", "blocked", "completed", "failed"]
EventType = Literal[
    "agent.status_changed",
    "analysis.started",
    "analysis.completed",
    "approval.required",
    "order.paper_submitted",
    "system.warning",
]


class AgentEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    event_type: EventType
    project_id: str = "investment-town"
    agent_id: str
    status: AgentStatus
    task: str | None = None
    ticker: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
