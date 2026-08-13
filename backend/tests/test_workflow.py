from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from investment_town.core.config import Settings
from investment_town.integrations.trading_agents import (
    TradingAgentsUnavailable,
    TradingProposal,
    analyze_with_trading_agents,
)
from investment_town.main import create_app
from investment_town.workflows.research import research_workflow_outline


class FakeTradingGraph:
    def __init__(self) -> None:
        self.call: tuple[str, str, str] | None = None

    def propagate(
        self, company_name: str, trade_date: str, asset_type: str = "stock"
    ) -> tuple[dict[str, str], str]:
        self.call = (company_name, trade_date, asset_type)
        return {"final_trade_decision": "**Rating**: Overweight"}, "Overweight"


def test_research_workflow_is_paper_only() -> None:
    plan = research_workflow_outline("nvda")
    assert plan["ticker"] == "NVDA"
    assert plan["execution_mode"] == "paper"
    assert plan["stages"][-1] == ["portfolio_manager"]


def test_trading_agents_result_is_a_proposal_only() -> None:
    graph = FakeTradingGraph()
    proposal = analyze_with_trading_agents("nvda", date(2026, 8, 13), graph=graph)

    assert graph.call == ("NVDA", "2026-08-13", "stock")
    assert proposal.rating == "Overweight"
    assert proposal.suggested_paper_action == "buy"
    assert proposal.human_approval_required is True
    assert proposal.approval_status == "pending"
    assert proposal.order_created is False


def test_research_proposal_api_persists(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "control.db"

    def fake_analysis(ticker: str, analysis_date: date) -> TradingProposal:
        return TradingProposal(
            ticker=ticker,
            analysis_date=analysis_date,
            rating="Buy",
            suggested_paper_action="buy",
            report="Paper proposal only.",
        )

    monkeypatch.setattr(
        "investment_town.api.router.analyze_with_trading_agents", fake_analysis
    )
    app = create_app(Settings(database_path=str(database)))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/research/proposals",
            json={"ticker": "nvda", "analysis_date": "2026-08-13"},
        )
        assert created.status_code == 201
        assert created.json()["ticker"] == "NVDA"
        assert created.json()["approval_status"] == "pending"
        assert created.json()["order_created"] is False

    reopened = create_app(Settings(database_path=str(database)))
    with TestClient(reopened) as client:
        proposals = client.get("/api/v1/research/proposals").json()
        assert len(proposals) == 1
        assert proposals[0]["report"] == "Paper proposal only."


def test_research_proposal_reports_missing_optional_engine(tmp_path: Path, monkeypatch) -> None:
    def unavailable(ticker: str, analysis_date: date) -> TradingProposal:
        raise TradingAgentsUnavailable("install the backend 'agents' extra")

    monkeypatch.setattr("investment_town.api.router.analyze_with_trading_agents", unavailable)
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    with TestClient(app) as client:
        response = client.post("/api/v1/research/proposals", json={"ticker": "NVDA"})
        assert response.status_code == 503
        assert "agents" in response.json()["detail"]


def test_dashboard_exposes_agent_proposal_form(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "MVP 1.2A Agent Proposal" in response.text
        assert 'id="research-form"' in response.text


def test_project_command_persists_event_and_audit(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/projects/investment-town/commands/start",
            json={"reason": "test run"},
        )
        assert response.status_code == 200
        assert response.json()["project"]["state"] == "running"
        assert client.get("/api/v1/events").json()[0]["payload"]["command"] == "start"
        assert client.get("/api/v1/audit").json()[0]["reason"] == "test run"


def test_invalid_transition_and_token_auth(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            app_env="test",
            database_path=str(tmp_path / "control.db"),
            control_api_token="secret-token",
        )
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/projects").status_code == 401
        assert client.get(
            "/api/v1/paper/portfolio", params={"project_id": "investment-town"}
        ).status_code == 401
        headers = {"Authorization": "Bearer secret-token"}
        assert client.post(
            "/api/v1/projects/investment-town/commands/start", json={}, headers=headers
        ).status_code == 200
        response = client.post(
            "/api/v1/projects/investment-town/commands/start", json={}, headers=headers
        )
        assert response.status_code == 409
        assert "cannot start" in response.json()["detail"]


def test_production_requires_control_token(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            app_env="production",
            database_path=str(tmp_path / "control.db"),
            control_api_token=None,
        )
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/projects").status_code == 503


def test_websocket_receives_status_event(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    with TestClient(app) as client, client.websocket_connect("/api/v1/events/stream") as websocket:
        client.post("/api/v1/projects/investment-town/commands/start", json={})
        event = websocket.receive_json()
        assert event["event_type"] == "project.status_changed"
        assert event["payload"]["to_state"] == "running"


def test_paper_buy_sell_and_persistence(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    app = create_app(Settings(database_path=str(database)))
    with TestClient(app) as client:
        assert client.get(
            "/api/v1/paper/portfolio", params={"project_id": "investment-town"}
        ).json()["cash"] == "100000"
        client.post("/api/v1/projects/investment-town/commands/start", json={})
        buy = client.post(
            "/api/v1/paper/orders",
            json={"ticker": "nvda", "side": "buy", "quantity": 10, "price": "100.00"},
        )
        assert buy.status_code == 200
        assert buy.json()["portfolio"]["cash"] == "99000"
        assert buy.json()["portfolio"]["positions"][0]["ticker"] == "NVDA"
        sell = client.post(
            "/api/v1/paper/orders",
            json={"ticker": "NVDA", "side": "sell", "quantity": 4, "price": "125.00"},
        )
        assert sell.status_code == 200
        assert sell.json()["portfolio"]["realized_pnl"] == "100"
        assert sell.json()["portfolio"]["positions"][0]["quantity"] == 6
        assert client.get("/api/v1/events").json()[0]["event_type"] == "paper.order.filled"

    reopened = create_app(Settings(database_path=str(database)))
    with TestClient(reopened) as client:
        portfolio = client.get(
            "/api/v1/paper/portfolio", params={"project_id": "investment-town"}
        ).json()
        assert portfolio["cash"] == "99500"
        assert portfolio["positions"][0]["quantity"] == 6
        trades = client.get(
            "/api/v1/paper/trades", params={"project_id": "investment-town"}
        ).json()
        assert len(trades) == 2


def test_paper_order_requires_running_project_and_balance(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    with TestClient(app) as client:
        order = {"ticker": "NVDA", "side": "buy", "quantity": 1, "price": "100.00"}
        assert client.post("/api/v1/paper/orders", json=order).status_code == 409
        client.post("/api/v1/projects/investment-town/commands/start", json={})
        order["quantity"] = 1001
        assert client.post("/api/v1/paper/orders", json=order).status_code == 409
        sell = {"ticker": "NVDA", "side": "sell", "quantity": 1, "price": "100.00"}
        assert client.post("/api/v1/paper/orders", json=sell).status_code == 409
