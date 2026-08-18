import asyncio
import secrets
from decimal import Decimal
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from investment_town.agents.registry import AGENT_ROLES
from investment_town.broker.paper import (
    InvalidPaperOrder,
    PaperOrderRequest,
    PaperOrderResult,
    PaperPortfolio,
    PaperTrade,
)
from investment_town.control import (
    InvalidProposalDecision,
    InvalidResearchRun,
    InvalidTransition,
    ProjectControl,
)
from investment_town.core.config import Settings
from investment_town.integrations.trading_agents import (
    ProposalApprovalRequest,
    ProposalRejectionRequest,
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
from investment_town.schemas.research import ResearchRun, ResearchRunDetail
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


class ProposalDecisionResult(BaseModel):
    proposal: TradingProposal
    trade: PaperTrade | None
    event: ControlEvent


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


@router.post("/research/runs", response_model=ResearchRun, status_code=202)
async def create_research_run(
    body: ResearchAnalysisRequest,
    background_tasks: BackgroundTasks,
    _: Actor,
    control: Control,
) -> ResearchRun:
    try:
        run = control.start_research_run(body.ticker, body.analysis_date)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found") from error
    except InvalidResearchRun as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    background_tasks.add_task(control.execute_research_run, str(run.run_id))
    return run


@router.get("/research/runs", response_model=list[ResearchRun])
def research_runs(
    _: Actor,
    control: Control,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[ResearchRun]:
    return control.store.list_research_runs(limit)


@router.get("/research/runs/{run_id}", response_model=ResearchRunDetail)
def research_run(run_id: str, _: Actor, control: Control) -> ResearchRunDetail:
    detail = control.store.get_research_run_detail(run_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "research run not found")
    return detail


def _change_research_run(
    control: ProjectControl,
    run_id: str,
    action: str,
) -> ResearchRunDetail:
    try:
        if action == "pause":
            return control.pause_research_run(run_id)
        if action == "resume":
            return control.resume_research_run(run_id)
        return control.retry_research_run(run_id)
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "research run not found") from error
    except InvalidResearchRun as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error


@router.post("/research/runs/{run_id}/pause", response_model=ResearchRunDetail)
def pause_research_run(run_id: str, _: Actor, control: Control) -> ResearchRunDetail:
    return _change_research_run(control, run_id, "pause")


@router.post("/research/runs/{run_id}/resume", response_model=ResearchRunDetail)
async def resume_research_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    _: Actor,
    control: Control,
) -> ResearchRunDetail:
    detail = _change_research_run(control, run_id, "resume")
    background_tasks.add_task(control.execute_research_run, run_id)
    return detail


@router.post("/research/runs/{run_id}/retry", response_model=ResearchRunDetail)
async def retry_research_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    _: Actor,
    control: Control,
) -> ResearchRunDetail:
    detail = _change_research_run(control, run_id, "retry")
    background_tasks.add_task(control.execute_research_run, run_id)
    return detail


@router.get("/research/proposals", response_model=list[TradingProposal])
def research_proposals(
    _: Actor,
    control: Control,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[TradingProposal]:
    return control.store.list_research_proposals(limit)


@router.get("/research/proposals/{proposal_id}", response_model=TradingProposal)
def research_proposal(proposal_id: str, _: Actor, control: Control) -> TradingProposal:
    proposal = control.store.get_research_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    return proposal


async def _decide_proposal(
    control: ProjectControl,
    proposal_id: str,
    *,
    approve: bool,
    actor: str,
    reason: str | None,
    quantity: int | None = None,
    price: Decimal | None = None,
) -> ProposalDecisionResult:
    try:
        proposal, order, event = await control.decide_proposal(
            proposal_id,
            approve=approve,
            actor=actor,
            reason=reason,
            quantity=quantity,
            price=price,
        )
    except KeyError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found") from error
    except (InvalidProposalDecision, InvalidPaperOrder) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return ProposalDecisionResult(
        proposal=proposal,
        trade=order.trade if order else None,
        event=event,
    )


@router.post(
    "/research/proposals/{proposal_id}/approve",
    response_model=ProposalDecisionResult,
)
async def approve_research_proposal(
    proposal_id: str,
    body: ProposalApprovalRequest,
    actor: Actor,
    control: Control,
) -> ProposalDecisionResult:
    return await _decide_proposal(
        control,
        proposal_id,
        approve=True,
        actor=actor,
        reason=body.reason,
        quantity=body.quantity,
        price=body.price,
    )


@router.post(
    "/research/proposals/{proposal_id}/reject",
    response_model=ProposalDecisionResult,
)
async def reject_research_proposal(
    proposal_id: str,
    body: ProposalRejectionRequest,
    actor: Actor,
    control: Control,
) -> ProposalDecisionResult:
    return await _decide_proposal(
        control,
        proposal_id,
        approve=False,
        actor=actor,
        reason=body.reason,
    )


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
