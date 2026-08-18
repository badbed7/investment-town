from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

Rating = Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
PaperAction = Literal["buy", "hold", "sell"]
ApprovalStatus = Literal["pending", "approved", "rejected"]

_ACTIONS: dict[Rating, PaperAction] = {
    "Buy": "buy",
    "Overweight": "buy",
    "Hold": "hold",
    "Underweight": "sell",
    "Sell": "sell",
}


class TradingGraphRunner(Protocol):
    def propagate(
        self, company_name: str, trade_date: str, asset_type: str = "stock"
    ) -> tuple[dict[str, Any], str]: ...


class AgentResearchOutput(BaseModel):
    content: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    model: str | None = None
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    estimated_cost: Decimal = Field(default=Decimal(0), ge=0)


class TradingAnalysisResult(BaseModel):
    proposal: "TradingProposal"
    agent_outputs: dict[str, AgentResearchOutput]


class ResearchAnalysisRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9.-]+$")
    analysis_date: date = Field(default_factory=date.today)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()


class TradingProposal(BaseModel):
    proposal_id: UUID = Field(default_factory=uuid4)
    project_id: str = "investment-town"
    ticker: str
    analysis_date: date
    rating: Rating
    suggested_paper_action: PaperAction
    report: str
    source: str = "TauricResearch/TradingAgents"
    human_approval_required: bool = True
    approval_status: ApprovalStatus = "pending"
    order_created: bool = False
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    trade_id: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProposalApprovalRequest(BaseModel):
    quantity: int | None = Field(default=None, gt=0, le=1_000_000)
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    reason: str | None = Field(default=None, max_length=500)


class ProposalRejectionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class TradingAgentsUnavailable(RuntimeError):
    pass


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _nested_text(state: dict[str, Any], *path: str) -> str:
    value: Any = state
    for key in path:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return _as_text(value)


def _agent_outputs(state: dict[str, Any]) -> dict[str, AgentResearchOutput]:
    candidates: dict[str, tuple[tuple[str, ...], ...]] = {
        "news": (("news_report",),),
        "fundamental": (("fundamentals_report",), ("fundamental_report",)),
        "macro": (("sentiment_report",), ("macro_report",)),
        "quant": (("market_report",), ("technical_report",)),
        "bull": (
            ("investment_debate_state", "bull_history"),
            ("investment_debate_state", "bull_research"),
        ),
        "bear": (
            ("investment_debate_state", "bear_history"),
            ("investment_debate_state", "bear_research"),
        ),
        "risk": (
            ("risk_debate_state", "judge_decision"),
            ("risk_debate_state", "risk_decision"),
        ),
        "portfolio_manager": (
            ("final_trade_decision",),
            ("trader_investment_plan",),
        ),
    }
    metadata = state.get("agent_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    outputs: dict[str, AgentResearchOutput] = {}
    for agent_id, paths in candidates.items():
        content = next(
            (text for path in paths if (text := _nested_text(state, *path))), ""
        )
        raw_metadata = metadata.get(agent_id)
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}
        raw_evidence = raw_metadata.get("evidence_ids", [])
        evidence_ids = (
            [str(item) for item in raw_evidence]
            if isinstance(raw_evidence, list)
            else []
        )
        outputs[agent_id] = AgentResearchOutput(
            content=content,
            evidence_ids=evidence_ids,
            confidence=raw_metadata.get("confidence"),
            model=_as_text(raw_metadata.get("model")) or None,
            prompt_tokens=raw_metadata.get("prompt_tokens", 0),
            completion_tokens=raw_metadata.get("completion_tokens", 0),
            estimated_cost=raw_metadata.get("estimated_cost", "0"),
        )
    return outputs


def run_trading_agents_analysis(
    ticker: str,
    analysis_date: date,
    *,
    graph: TradingGraphRunner | None = None,
) -> TradingAnalysisResult:
    """Run TradingAgents and normalize its durable Agent outputs without trading."""
    if graph is None:
        try:
            from tradingagents.graph.trading_graph import TradingAgentsGraph
        except ImportError as error:
            raise TradingAgentsUnavailable(
                "install the backend 'agents' extra before running TradingAgents"
            ) from error
        graph = TradingAgentsGraph(debug=False)

    state, raw_rating = graph.propagate(ticker.upper(), analysis_date.isoformat())
    rating = raw_rating.title()
    if rating not in _ACTIONS:
        raise ValueError(f"unsupported TradingAgents rating: {raw_rating!r}")

    proposal = TradingProposal(
        ticker=ticker.upper(),
        analysis_date=analysis_date,
        rating=rating,
        suggested_paper_action=_ACTIONS[rating],
        report=_as_text(state.get("final_trade_decision")),
    )
    return TradingAnalysisResult(proposal=proposal, agent_outputs=_agent_outputs(state))


def analyze_with_trading_agents(
    ticker: str,
    analysis_date: date,
    *,
    graph: TradingGraphRunner | None = None,
) -> TradingProposal:
    """Compatibility wrapper returning only the human-gated proposal."""
    return run_trading_agents_analysis(ticker, analysis_date, graph=graph).proposal
