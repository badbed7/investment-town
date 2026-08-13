import asyncio
import secrets
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from investment_town.agents.registry import AGENT_ROLES
from investment_town.broker.paper import (
    InvalidPaperOrder,
    PaperOrderRequest,
    PaperOrderResult,
    PaperPortfolio,
    PaperTrade,
)
from investment_town.control import InvalidTransition, ProjectControl
from investment_town.core.config import Settings
from investment_town.integrations.trading_agents import (
    ResearchAnalysisRequest,
    TradingAgentsUnavailable,
    TradingProposal,
    analyze_with_trading_agents,
)
from investment_town.schemas.commands import ProjectCommand, ProjectCommandName
from investment_town.schemas.control import (
    AuditEntry,
    CommandRequest,
    CommandResult,
    ControlEvent,
    Project,
)
from investment_town.workflows.research import research_workflow_outline

router = APIRouter(prefix="/api/v1")
bearer = HTTPBearer(auto_error=False)


def _control(request: Request) -> ProjectControl:
    return request.app.state.control


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def authorize(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    app_settings = _settings(request)
    if app_settings.control_api_token is None:
        if app_settings.app_env == "development":
            return "local-development"
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "CONTROL_API_TOKEN is required")
    if credentials is None or not secrets.compare_digest(
        credentials.credentials, app_settings.control_api_token
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid control token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "operator"


Actor = Annotated[str, Depends(authorize)]
Control = Annotated[ProjectControl, Depends(_control)]


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    app_settings = _settings(request)
    return {
        "status": "ok",
        "environment": app_settings.app_env,
        "paper_trading_only": app_settings.paper_trading_only,
        "live_trading_implemented": False,
    }


@router.get("/agents")
def agents(_: Actor) -> dict[str, object]:
    return {"agents": list(AGENT_ROLES)}


@router.get("/research/{ticker}/plan")
def research_plan(ticker: str, _: Actor) -> dict[str, object]:
    return research_workflow_outline(ticker)


@router.post("/research/proposals", response_model=TradingProposal, status_code=201)
async def create_research_proposal(
    body: ResearchAnalysisRequest,
    _: Actor,
    control: Control,
) -> TradingProposal:
    try:
        proposal = await asyncio.to_thread(
            analyze_with_trading_agents, body.ticker, body.analysis_date
        )
    except TradingAgentsUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
    except Exception as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "TradingAgents analysis failed"
        ) from error
    return control.store.save_research_proposal(proposal)


@router.get("/research/proposals", response_model=list[TradingProposal])
def research_proposals(
    _: Actor,
    control: Control,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[TradingProposal]:
    return control.store.list_research_proposals(limit)


@router.get("/projects", response_model=list[Project])
def projects(_: Actor, control: Control) -> list[Project]:
    return control.store.list_projects()


@router.get("/projects/{project_id}", response_model=Project)
def project(project_id: str, _: Actor, control: Control) -> Project:
    found = control.store.get_project(project_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return found


async def _run_command(
    control: ProjectControl,
    project_id: str,
    command: ProjectCommandName,
    actor: str,
    reason: str | None,
) -> CommandResult:
    try:
        return await control.command(project_id, command, actor, reason)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found") from error
    except InvalidTransition as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error


@router.post("/projects/{project_id}/commands/{command}", response_model=CommandResult)
async def project_command(
    project_id: str,
    command: ProjectCommandName,
    body: CommandRequest,
    actor: Actor,
    control: Control,
) -> CommandResult:
    return await _run_command(control, project_id, command, actor, body.reason)


@router.post("/projects/command", response_model=CommandResult, deprecated=True)
async def legacy_project_command(
    body: ProjectCommand, actor: Actor, control: Control
) -> CommandResult:
    return await _run_command(control, body.project_id, body.command, actor, body.reason)


@router.get("/events", response_model=list[ControlEvent])
def events(
    _: Actor,
    control: Control,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ControlEvent]:
    return control.store.list_events(limit)


@router.get("/audit", response_model=list[AuditEntry])
def audit(
    _: Actor,
    control: Control,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditEntry]:
    return control.store.list_audit(limit)


@router.websocket("/events/stream")
async def event_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    app_settings: Settings = websocket.app.state.settings
    if app_settings.control_api_token is not None:
        try:
            auth = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        except (TimeoutError, ValueError, WebSocketDisconnect):
            await websocket.close(code=4401, reason="authentication required")
            return
        token = auth.get("token") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not secrets.compare_digest(
            token, app_settings.control_api_token
        ):
            await websocket.close(code=4401, reason="invalid control token")
            return

    control: ProjectControl = websocket.app.state.control
    queue = control.events.subscribe()
    try:
        for event in reversed(control.store.list_events(20)):
            await websocket.send_json(event.model_dump(mode="json"))
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
                await websocket.send_json(event.model_dump(mode="json"))
            except TimeoutError:
                await websocket.send_json({"event_type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    finally:
        control.events.unsubscribe(queue)


@router.get("/paper/portfolio", response_model=PaperPortfolio)
def paper_portfolio(
    project_id: str,
    _: Actor,
    control: Control,
) -> PaperPortfolio:
    try:
        return control.paper.get_portfolio(project_id)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found") from error


@router.get("/paper/trades", response_model=list[PaperTrade])
def paper_trades(
    project_id: str,
    _: Actor,
    control: Control,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[PaperTrade]:
    try:
        return control.paper.list_trades(project_id, limit)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found") from error


@router.post("/paper/orders", response_model=PaperOrderResult)
async def paper_order(
    body: PaperOrderRequest,
    _: Actor,
    control: Control,
) -> PaperOrderResult:
    try:
        return await control.paper_order(body)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found") from error
    except InvalidPaperOrder as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
