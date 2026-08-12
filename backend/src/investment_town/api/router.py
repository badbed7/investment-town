from fastapi import APIRouter

from investment_town.agents.registry import AGENT_ROLES
from investment_town.core.config import settings
from investment_town.schemas.commands import ProjectCommand
from investment_town.workflows.research import research_workflow_outline

router = APIRouter(prefix="/api/v1")


@router.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "environment": settings.app_env,
        "paper_trading_only": settings.paper_trading_only,
    }


@router.get("/agents")
def agents() -> dict[str, object]:
    return {"agents": list(AGENT_ROLES)}


@router.get("/research/{ticker}/plan")
def research_plan(ticker: str) -> dict[str, object]:
    return research_workflow_outline(ticker)


@router.post("/projects/command")
def project_command(command: ProjectCommand) -> dict[str, object]:
    # MVP stub: command persistence/orchestration is added in a later milestone.
    return {"accepted": True, "command": command.model_dump()}
