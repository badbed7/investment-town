from pathlib import Path

from fastapi.testclient import TestClient

from investment_town.core.config import Settings
from investment_town.main import create_app
from investment_town.workflows.research import research_workflow_outline


def test_research_workflow_is_paper_only() -> None:
    plan = research_workflow_outline("nvda")
    assert plan["ticker"] == "NVDA"
    assert plan["execution_mode"] == "paper"
    assert plan["stages"][-1] == ["portfolio_manager"]


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
