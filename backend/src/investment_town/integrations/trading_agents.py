from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

Rating = Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
PaperAction = Literal["buy", "hold", "sell"]

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
    approval_status: Literal["pending"] = "pending"
    order_created: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TradingAgentsUnavailable(RuntimeError):
    pass


def analyze_with_trading_agents(
    ticker: str,
    analysis_date: date,
    *,
    graph: TradingGraphRunner | None = None,
) -> TradingProposal:
    """Run TradingAgents and return a proposal without submitting an order."""
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

    return TradingProposal(
        ticker=ticker.upper(),
        analysis_date=analysis_date,
        rating=rating,
        suggested_paper_action=_ACTIONS[rating],
        report=str(state.get("final_trade_decision", "")),
    )
