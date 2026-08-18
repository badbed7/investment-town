import sqlite3
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from investment_town.core.config import Settings
from investment_town.integrations.trading_agents import (
    TradingAgentsUnavailable,
    TradingAnalysisResult,
    TradingProposal,
    analyze_with_trading_agents,
    run_trading_agents_analysis,
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


def test_trading_agents_state_is_normalized_for_the_blackboard() -> None:
    class FakeResearchGraph:
        def propagate(
            self, company_name: str, trade_date: str, asset_type: str = "stock"
        ) -> tuple[dict[str, object], str]:
            return (
                {
                    "news_report": "Material product announcement.",
                    "fundamentals_report": "Revenue growth remains strong.",
                    "sentiment_report": "Risk appetite is neutral.",
                    "market_report": "Momentum is positive.",
                    "investment_debate_state": {
                        "bull_history": "Bull case.",
                        "bear_history": "Bear case.",
                    },
                    "risk_debate_state": {"judge_decision": "Risk is acceptable."},
                    "final_trade_decision": "Final portfolio decision.",
                },
                "Buy",
            )

    result = run_trading_agents_analysis(
        "nvda", date(2026, 8, 19), graph=FakeResearchGraph()
    )

    assert result.proposal.rating == "Buy"
    assert result.agent_outputs["news"] == "Material product announcement."
    assert result.agent_outputs["bull"] == "Bull case."
    assert result.agent_outputs["risk"] == "Risk is acceptable."
    assert result.agent_outputs["portfolio_manager"] == "Final portfolio decision."


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


def test_approved_agent_proposal_creates_exactly_one_paper_order(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    proposal = TradingProposal(
        ticker="NVDA",
        analysis_date=date(2026, 8, 18),
        rating="Buy",
        suggested_paper_action="buy",
        report="Approval test.",
    )

    with TestClient(app) as client:
        client.app.state.control.store.save_research_proposal(proposal)
        client.post("/api/v1/projects/investment-town/commands/start", json={})
        approved = client.post(
            f"/api/v1/research/proposals/{proposal.proposal_id}/approve",
            json={"quantity": 3, "price": "125.50", "reason": "human reviewed"},
        )

        assert approved.status_code == 200
        result = approved.json()
        assert result["proposal"]["approval_status"] == "approved"
        assert result["proposal"]["order_created"] is True
        assert result["proposal"]["trade_id"] == result["trade"]["trade_id"]
        assert result["trade"]["quantity"] == 3
        assert result["event"]["event_type"] == "research.proposal.approved"
        assert client.get(
            "/api/v1/paper/portfolio", params={"project_id": "investment-town"}
        ).json()["positions"][0]["quantity"] == 3

        duplicate = client.post(
            f"/api/v1/research/proposals/{proposal.proposal_id}/approve",
            json={"quantity": 3, "price": "125.50"},
        )
        assert duplicate.status_code == 409
        assert len(
            client.get(
                "/api/v1/paper/trades", params={"project_id": "investment-town"}
            ).json()
        ) == 1


def test_rejected_agent_proposal_cannot_create_order(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    proposal = TradingProposal(
        ticker="TSLA",
        analysis_date=date(2026, 8, 18),
        rating="Sell",
        suggested_paper_action="sell",
        report="Reject test.",
    )

    with TestClient(app) as client:
        client.app.state.control.store.save_research_proposal(proposal)
        rejected = client.post(
            f"/api/v1/research/proposals/{proposal.proposal_id}/reject",
            json={"reason": "evidence is insufficient"},
        )

        assert rejected.status_code == 200
        result = rejected.json()
        assert result["proposal"]["approval_status"] == "rejected"
        assert result["proposal"]["order_created"] is False
        assert result["proposal"]["decision_reason"] == "evidence is insufficient"
        assert result["trade"] is None
        assert result["event"]["event_type"] == "research.proposal.rejected"

        client.post("/api/v1/projects/investment-town/commands/start", json={})
        later_approval = client.post(
            f"/api/v1/research/proposals/{proposal.proposal_id}/approve",
            json={"quantity": 1, "price": "100"},
        )
        assert later_approval.status_code == 409


def test_hold_proposal_approval_records_decision_without_order(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    proposal = TradingProposal(
        ticker="MSFT",
        analysis_date=date(2026, 8, 18),
        rating="Hold",
        suggested_paper_action="hold",
        report="No position change.",
    )

    with TestClient(app) as client:
        client.app.state.control.store.save_research_proposal(proposal)
        approved = client.post(
            f"/api/v1/research/proposals/{proposal.proposal_id}/approve",
            json={"reason": "hold thesis accepted"},
        )

        assert approved.status_code == 200
        assert approved.json()["proposal"]["approval_status"] == "approved"
        assert approved.json()["proposal"]["order_created"] is False
        assert approved.json()["trade"] is None


def test_buy_proposal_requires_terms_and_running_project(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    proposal = TradingProposal(
        ticker="NVDA",
        analysis_date=date(2026, 8, 18),
        rating="Buy",
        suggested_paper_action="buy",
        report="Terms test.",
    )

    with TestClient(app) as client:
        client.app.state.control.store.save_research_proposal(proposal)
        missing_terms = client.post(
            f"/api/v1/research/proposals/{proposal.proposal_id}/approve", json={}
        )
        assert missing_terms.status_code == 409

        stopped_project = client.post(
            f"/api/v1/research/proposals/{proposal.proposal_id}/approve",
            json={"quantity": 1, "price": "100"},
        )
        assert stopped_project.status_code == 409
        restored = client.get(
            f"/api/v1/research/proposals/{proposal.proposal_id}"
        ).json()
        assert restored["approval_status"] == "pending"
        assert restored["order_created"] is False


def test_existing_proposal_database_is_migrated_in_place(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    proposal = TradingProposal(
        ticker="NVDA",
        analysis_date=date(2026, 8, 18),
        rating="Hold",
        suggested_paper_action="hold",
        report="Stored before the approval MVP.",
    )
    values = proposal.model_dump(mode="json")
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            """
            CREATE TABLE research_proposals (
                proposal_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                analysis_date TEXT NOT NULL,
                rating TEXT NOT NULL,
                suggested_paper_action TEXT NOT NULL,
                report TEXT NOT NULL,
                source TEXT NOT NULL,
                human_approval_required INTEGER NOT NULL,
                approval_status TEXT NOT NULL,
                order_created INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO research_proposals VALUES (
                :proposal_id, :project_id, :ticker, :analysis_date, :rating,
                :suggested_paper_action, :report, :source, :human_approval_required,
                :approval_status, :order_created, :created_at
            )
            """,
            values,
        )
    connection.close()

    app = create_app(Settings(database_path=str(database)))
    with TestClient(app) as client:
        restored = client.get(
            f"/api/v1/research/proposals/{proposal.proposal_id}"
        ).json()
        assert restored["approval_status"] == "pending"
        assert restored["decided_at"] is None
        assert restored["trade_id"] is None


def test_durable_research_run_records_agent_tasks_and_blackboard(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "control.db"

    def fake_analysis(ticker: str, analysis_date: date) -> TradingAnalysisResult:
        proposal = TradingProposal(
            ticker=ticker,
            analysis_date=analysis_date,
            rating="Overweight",
            suggested_paper_action="buy",
            report="Portfolio conclusion.",
        )
        return TradingAnalysisResult(
            proposal=proposal,
            agent_outputs={
                "news": "News result.",
                "fundamental": "Fundamental result.",
                "macro": "Macro result.",
                "quant": "Quant result.",
                "bull": "Bull result.",
                "bear": "Bear result.",
                "risk": "Risk result.",
                "portfolio_manager": "Portfolio conclusion.",
            },
        )

    monkeypatch.setattr(
        "investment_town.control.run_trading_agents_analysis", fake_analysis
    )
    app = create_app(Settings(database_path=str(database)))
    with TestClient(app) as client:
        client.post("/api/v1/projects/investment-town/commands/start", json={})
        created = client.post(
            "/api/v1/research/runs",
            json={"ticker": "nvda", "analysis_date": "2026-08-19"},
        )
        assert created.status_code == 202
        assert created.json()["status"] == "running"

        run_id = created.json()["run_id"]
        detail = client.get(f"/api/v1/research/runs/{run_id}").json()
        assert detail["run"]["status"] == "completed"
        assert detail["run"]["ticker"] == "NVDA"
        assert detail["run"]["final_rating"] == "Overweight"
        assert len(detail["tasks"]) == 8
        assert {task["status"] for task in detail["tasks"]} == {"completed"}
        assert len(detail["blackboard"]) == 8
        assert {entry["agent_id"] for entry in detail["blackboard"]} == {
            "news",
            "fundamental",
            "macro",
            "quant",
            "bull",
            "bear",
            "risk",
            "portfolio_manager",
        }
        assert client.get("/api/v1/research/proposals").json()[0]["proposal_id"] == detail[
            "run"
        ]["proposal_id"]

    reopened = create_app(Settings(database_path=str(database)))
    with TestClient(reopened) as client:
        restored = client.get(f"/api/v1/research/runs/{run_id}").json()
        assert restored["run"]["status"] == "completed"
        assert restored["blackboard"][0]["content"]


def test_research_run_failure_is_durable_and_does_not_create_proposal(
    tmp_path: Path, monkeypatch
) -> None:
    def failed_analysis(ticker: str, analysis_date: date) -> TradingAnalysisResult:
        raise RuntimeError("provider secret should not be persisted")

    monkeypatch.setattr(
        "investment_town.control.run_trading_agents_analysis", failed_analysis
    )
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    with TestClient(app) as client:
        stopped = client.post("/api/v1/research/runs", json={"ticker": "NVDA"})
        assert stopped.status_code == 409

        client.post("/api/v1/projects/investment-town/commands/start", json={})
        created = client.post("/api/v1/research/runs", json={"ticker": "NVDA"})
        detail = client.get(
            f"/api/v1/research/runs/{created.json()['run_id']}"
        ).json()
        assert detail["run"]["status"] == "failed"
        assert detail["run"]["error"] == "TradingAgents analysis failed"
        assert {task["status"] for task in detail["tasks"]} == {"failed"}
        assert detail["blackboard"] == []
        assert client.get("/api/v1/research/proposals").json() == []


def test_interrupted_research_run_is_failed_on_restart(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    app = create_app(Settings(database_path=str(database)))
    with TestClient(app) as client:
        client.post("/api/v1/projects/investment-town/commands/start", json={})
        run, _ = client.app.state.control.store.create_research_run(
            "NVDA", date(2026, 8, 19)
        )

    reopened = create_app(Settings(database_path=str(database)))
    with TestClient(reopened) as client:
        detail = client.get(f"/api/v1/research/runs/{run.run_id}").json()
        assert detail["run"]["status"] == "failed"
        assert "service restart" in detail["run"]["error"]


def test_project_kill_fails_active_research_run(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    with TestClient(app) as client:
        client.post("/api/v1/projects/investment-town/commands/start", json={})
        run, _ = client.app.state.control.store.create_research_run(
            "NVDA", date(2026, 8, 19)
        )

        killed = client.post(
            "/api/v1/projects/investment-town/commands/kill",
            json={"reason": "operator emergency stop"},
        )
        assert killed.status_code == 200
        detail = client.get(f"/api/v1/research/runs/{run.run_id}").json()
        assert detail["run"]["status"] == "failed"
        assert "project state killed" in detail["run"]["error"]
        assert client.get("/api/v1/research/proposals").json() == []


def test_dashboard_exposes_agent_proposal_form(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=str(tmp_path / "control.db")))
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "MVP 2A Durable Research Runs" in response.text
        assert 'id="research-form"' in response.text
        assert 'id="research-runs"' in response.text


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
