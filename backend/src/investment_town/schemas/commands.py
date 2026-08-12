from typing import Literal

from pydantic import BaseModel, Field

ProjectCommandName = Literal["start", "pause", "resume", "stop", "kill"]


class ProjectCommand(BaseModel):
    project_id: str = "investment-town"
    command: ProjectCommandName
    reason: str | None = Field(default=None, max_length=500)
