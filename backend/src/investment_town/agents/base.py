from dataclasses import dataclass
from typing import Protocol

from investment_town.schemas.events import AgentEvent


@dataclass(frozen=True, slots=True)
class AgentContext:
    ticker: str
    task: str


class Agent(Protocol):
    agent_id: str

    async def run(self, context: AgentContext) -> AgentEvent: ...
