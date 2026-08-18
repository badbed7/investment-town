import asyncio
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from uuid import uuid4

from investment_town.broker.paper import PaperOrderRequest, PaperOrderResult, PaperStore
from investment_town.integrations.trading_agents import (
    TradingAgentsUnavailable,
    TradingAnalysisResult,
    TradingProposal,
    run_trading_agents_analysis,
)
from investment_town.schemas.commands import ProjectCommandName
from investment_town.schemas.control import (
    AuditEntry,
    CommandResult,
    ControlEvent,
    Project,
    ProjectHealth,
    ProjectState,
)
from investment_town.schemas.research import (
    BlackboardEntry,
    ModelUsage,
    ResearchAgentTask,
    ResearchCheckpoint,
    ResearchRun,
    ResearchRunDetail,
)
from investment_town.workflows.research import RESEARCH_STAGES

TRANSITIONS: Mapping[ProjectCommandName, Mapping[ProjectState, ProjectState]] = {
    "start": {"idle": "running", "stopped": "running", "failed": "running"},
    "pause": {"running": "paused"},
    "resume": {"paused": "running"},
    "stop": {"running": "stopped", "paused": "stopped"},
    "kill": {
        "idle": "killed",
        "running": "killed",
        "paused": "killed",
        "stopped": "killed",
        "failed": "killed",
    },
}


class InvalidTransition(ValueError):
    pass


class InvalidProposalDecision(ValueError):
    pass


class InvalidResearchRun(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _health(state: ProjectState) -> ProjectHealth:
    if state == "failed":
        return "degraded"
    if state == "killed":
        return "halted"
    return "healthy"


class ProjectStore:
    """Small durable MVP store; replace with PostgreSQL when multi-instance runtime is needed."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        with self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    health TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    audit_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_proposals (
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
                    decided_at TEXT,
                    decided_by TEXT,
                    decision_reason TEXT,
                    trade_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_stage INTEGER NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    final_rating TEXT,
                    proposal_id TEXT,
                    error TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost TEXT NOT NULL DEFAULT '0',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS research_agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    stage INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    confidence REAL,
                    evidence_ids TEXT NOT NULL DEFAULT '[]',
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(run_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS blackboard_entries (
                    entry_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    content TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    stage INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    completed_agents TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_usage (
                    usage_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    model TEXT,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    estimated_cost TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS research_analysis_snapshots (
                    run_id TEXT PRIMARY KEY,
                    analysis TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_runs_created
                    ON research_runs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_blackboard_run
                    ON blackboard_entries(run_id, created_at);
                """
            )
            proposal_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(research_proposals)"
                ).fetchall()
            }
            for name, definition in {
                "decided_at": "TEXT",
                "decided_by": "TEXT",
                "decision_reason": "TEXT",
                "trade_id": "TEXT",
            }.items():
                if name not in proposal_columns:
                    self._connection.execute(
                        f"ALTER TABLE research_proposals ADD COLUMN {name} {definition}"
                    )
            run_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(research_runs)"
                ).fetchall()
            }
            for name, definition in {
                "attempt": "INTEGER NOT NULL DEFAULT 1",
                "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
                "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
                "estimated_cost": "TEXT NOT NULL DEFAULT '0'",
            }.items():
                if name not in run_columns:
                    self._connection.execute(
                        f"ALTER TABLE research_runs ADD COLUMN {name} {definition}"
                    )
            task_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(research_agent_tasks)"
                ).fetchall()
            }
            for name, definition in {
                "attempt": "INTEGER NOT NULL DEFAULT 1",
                "confidence": "REAL",
                "evidence_ids": "TEXT NOT NULL DEFAULT '[]'",
            }.items():
                if name not in task_columns:
                    self._connection.execute(
                        f"ALTER TABLE research_agent_tasks ADD COLUMN {name} {definition}"
                    )

    def close(self) -> None:
        self._connection.close()

    def register(self, project_id: str, name: str) -> None:
        now = _now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO projects(project_id, name, state, health, updated_at)
                VALUES (?, ?, 'idle', 'healthy', ?)
                """,
                (project_id, name, now),
            )

    def list_projects(self) -> list[Project]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM projects ORDER BY project_id"
            ).fetchall()
        return [Project.model_validate(dict(row)) for row in rows]

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return Project.model_validate(dict(row)) if row else None

    def apply_command(
        self,
        project_id: str,
        command: ProjectCommandName,
        actor: str,
        reason: str | None,
    ) -> CommandResult:
        request_id = uuid4()
        event_id = uuid4()
        audit_id = uuid4()
        created_at = _now()

        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise KeyError(project_id)

            from_state: ProjectState = row["state"]
            to_state = TRANSITIONS[command].get(from_state)
            if to_state is None:
                raise InvalidTransition(f"cannot {command} project from {from_state}")

            health = _health(to_state)
            payload = {
                "request_id": str(request_id),
                "command": command,
                "actor": actor,
                "reason": reason,
                "from_state": from_state,
                "to_state": to_state,
            }
            self._connection.execute(
                "UPDATE projects SET state = ?, health = ?, updated_at = ? WHERE project_id = ?",
                (to_state, health, created_at, project_id),
            )
            cursor = self._connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                VALUES (?, 'project.status_changed', ?, ?, ?)
                """,
                (str(event_id), project_id, json.dumps(payload), created_at),
            )
            self._connection.execute(
                """
                INSERT INTO audit_log(
                    audit_id, request_id, project_id, actor, action, reason,
                    from_state, to_state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(audit_id),
                    str(request_id),
                    project_id,
                    actor,
                    command,
                    reason,
                    from_state,
                    to_state,
                    created_at,
                ),
            )

        project = Project(
            project_id=project_id,
            name=row["name"],
            state=to_state,
            health=health,
            updated_at=created_at,
        )
        event = ControlEvent(
            sequence=cursor.lastrowid,
            event_id=event_id,
            event_type="project.status_changed",
            project_id=project_id,
            payload=payload,
            created_at=created_at,
        )
        audit = AuditEntry(
            audit_id=audit_id,
            request_id=request_id,
            project_id=project_id,
            actor=actor,
            action=command,
            reason=reason,
            from_state=from_state,
            to_state=to_state,
            created_at=created_at,
        )
        return CommandResult(project=project, event=event, audit=audit)

    def list_events(self, limit: int = 100) -> list[ControlEvent]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events ORDER BY sequence DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            ControlEvent.model_validate({**dict(row), "payload": json.loads(row["payload"])})
            for row in rows
        ]

    def list_audit(self, limit: int = 100) -> list[AuditEntry]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [AuditEntry.model_validate(dict(row)) for row in rows]

    def create_research_run(
        self,
        ticker: str,
        analysis_date: date,
        project_id: str = "investment-town",
    ) -> tuple[ResearchRun, ControlEvent]:
        run_id = uuid4()
        event_id = uuid4()
        created_at = _now()
        normalized_ticker = ticker.upper()

        with self._lock, self._connection:
            project = self._connection.execute(
                "SELECT state FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            if project["state"] != "running":
                raise InvalidResearchRun("project must be running to start a research run")

            self._connection.execute(
                """
                INSERT INTO research_runs(
                    run_id, project_id, ticker, analysis_date, status, current_stage,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', 0, ?, ?)
                """,
                (
                    str(run_id),
                    project_id,
                    normalized_ticker,
                    analysis_date.isoformat(),
                    created_at,
                    created_at,
                ),
            )
            for stage, agents in enumerate(RESEARCH_STAGES):
                for agent_id in agents:
                    self._connection.execute(
                        """
                        INSERT INTO research_agent_tasks(
                            task_id, run_id, agent_id, stage, status, summary
                        ) VALUES (?, ?, ?, ?, 'queued', '')
                        """,
                        (str(uuid4()), str(run_id), agent_id, stage),
                    )

            payload = {
                "run_id": str(run_id),
                "ticker": normalized_ticker,
                "status": "running",
            }
            cursor = self._connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                VALUES (?, 'research.run.started', ?, ?, ?)
                """,
                (str(event_id), project_id, json.dumps(payload), created_at),
            )

        run = ResearchRun(
            run_id=run_id,
            project_id=project_id,
            ticker=normalized_ticker,
            analysis_date=analysis_date,
            status="running",
            current_stage=0,
            created_at=created_at,
            updated_at=created_at,
        )
        event = ControlEvent(
            sequence=cursor.lastrowid,
            event_id=event_id,
            event_type="research.run.started",
            project_id=project_id,
            payload=payload,
            created_at=created_at,
        )
        return run, event

    def get_research_run(self, run_id: str) -> ResearchRun | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return ResearchRun.model_validate(dict(row)) if row else None

    def list_research_runs(self, limit: int = 30) -> list[ResearchRun]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM research_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [ResearchRun.model_validate(dict(row)) for row in rows]

    def get_research_run_detail(self, run_id: str) -> ResearchRunDetail | None:
        with self._lock:
            run_row = self._connection.execute(
                "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                return None
            task_rows = self._connection.execute(
                """
                SELECT * FROM research_agent_tasks
                WHERE run_id = ? ORDER BY stage, agent_id
                """,
                (run_id,),
            ).fetchall()
            entry_rows = self._connection.execute(
                """
                SELECT * FROM blackboard_entries
                WHERE run_id = ? ORDER BY created_at, agent_id
                """,
                (run_id,),
            ).fetchall()
            checkpoint_rows = self._connection.execute(
                """
                SELECT * FROM research_checkpoints
                WHERE run_id = ? ORDER BY created_at, stage
                """,
                (run_id,),
            ).fetchall()
            usage_rows = self._connection.execute(
                """
                SELECT * FROM model_usage
                WHERE run_id = ? ORDER BY agent_id
                """,
                (run_id,),
            ).fetchall()
        return ResearchRunDetail(
            run=ResearchRun.model_validate(dict(run_row)),
            tasks=[
                ResearchAgentTask.model_validate(
                    {**dict(row), "evidence_ids": json.loads(row["evidence_ids"])}
                )
                for row in task_rows
            ],
            blackboard=[
                BlackboardEntry.model_validate(
                    {**dict(row), "payload": json.loads(row["payload"])}
                )
                for row in entry_rows
            ],
            checkpoints=[
                ResearchCheckpoint.model_validate(
                    {**dict(row), "completed_agents": json.loads(row["completed_agents"])}
                )
                for row in checkpoint_rows
            ],
            usage=[ModelUsage.model_validate(dict(row)) for row in usage_rows],
        )

    def save_research_analysis_snapshot(
        self, run_id: str, analysis: TradingAnalysisResult
    ) -> None:
        created_at = _now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] not in {"running", "paused"}:
                return
            self._connection.execute(
                """
                INSERT INTO research_analysis_snapshots(run_id, analysis, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    analysis = excluded.analysis,
                    created_at = excluded.created_at
                """,
                (run_id, analysis.model_dump_json(), created_at),
            )

    def get_research_analysis_snapshot(
        self, run_id: str
    ) -> TradingAnalysisResult | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT analysis FROM research_analysis_snapshots WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return TradingAnalysisResult.model_validate_json(row["analysis"]) if row else None

    def process_next_research_stage(
        self, run_id: str
    ) -> tuple[ResearchRunDetail, list[ControlEvent], bool]:
        processed_at = _now()
        events: list[ControlEvent] = []

        with self._lock, self._connection:
            run_row = self._connection.execute(
                "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            if run_row["status"] != "running":
                raise InvalidResearchRun(f"research run is {run_row['status']}")
            snapshot_row = self._connection.execute(
                "SELECT analysis FROM research_analysis_snapshots WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if snapshot_row is None:
                raise InvalidResearchRun("research analysis snapshot is not available")
            analysis = TradingAnalysisResult.model_validate_json(snapshot_row["analysis"])
            stage = run_row["current_stage"]
            if stage < 0 or stage >= len(RESEARCH_STAGES):
                raise InvalidResearchRun(f"invalid research stage {stage}")

            task_rows = self._connection.execute(
                """
                SELECT * FROM research_agent_tasks
                WHERE run_id = ? AND stage = ? ORDER BY agent_id
                """,
                (run_id, stage),
            ).fetchall()
            completed_agents: list[str] = []
            for task in task_rows:
                output = analysis.agent_outputs.get(task["agent_id"])
                content = output.content.strip() if output else ""
                task_status = "completed" if content else "skipped"
                summary = (
                    " ".join(content.split())[:500]
                    if content
                    else "No structured output returned by the research engine."
                )
                confidence = output.confidence if output else None
                evidence_ids = output.evidence_ids if output else []
                self._connection.execute(
                    """
                    UPDATE research_agent_tasks
                    SET status = ?, summary = ?, attempt = ?, confidence = ?,
                        evidence_ids = ?, started_at = COALESCE(started_at, ?),
                        completed_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        task_status,
                        summary,
                        run_row["attempt"],
                        confidence,
                        json.dumps(evidence_ids),
                        processed_at,
                        processed_at,
                        task["task_id"],
                    ),
                )
                completed_agents.append(task["agent_id"])

                entry_id: str | None = None
                if content:
                    entry_id = str(uuid4())
                    entry_payload = {
                        "stage": stage,
                        "source": analysis.proposal.source,
                        "evidence_ids": evidence_ids,
                        "confidence": confidence,
                    }
                    self._connection.execute(
                        """
                        INSERT INTO blackboard_entries(
                            entry_id, run_id, agent_id, topic, content, payload, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entry_id,
                            run_id,
                            task["agent_id"],
                            f"{analysis.proposal.ticker} research",
                            content,
                            json.dumps(entry_payload),
                            processed_at,
                        ),
                    )

                if output and (
                    output.model
                    or output.prompt_tokens
                    or output.completion_tokens
                    or output.estimated_cost
                ):
                    self._connection.execute(
                        """
                        INSERT INTO model_usage(
                            usage_id, run_id, agent_id, model, prompt_tokens,
                            completion_tokens, estimated_cost, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(run_id, agent_id) DO UPDATE SET
                            model = excluded.model,
                            prompt_tokens = excluded.prompt_tokens,
                            completion_tokens = excluded.completion_tokens,
                            estimated_cost = excluded.estimated_cost,
                            created_at = excluded.created_at
                        """,
                        (
                            str(uuid4()),
                            run_id,
                            task["agent_id"],
                            output.model,
                            output.prompt_tokens,
                            output.completion_tokens,
                            str(output.estimated_cost),
                            processed_at,
                        ),
                    )

                event_id = uuid4()
                payload = {
                    "run_id": run_id,
                    "ticker": analysis.proposal.ticker,
                    "agent_id": task["agent_id"],
                    "stage": stage,
                    "status": task_status,
                    "blackboard_entry_id": entry_id,
                    "confidence": confidence,
                    "evidence_count": len(evidence_ids),
                }
                event_cursor = self._connection.execute(
                    """
                    INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                    VALUES (?, 'research.agent.finished', ?, ?, ?)
                    """,
                    (
                        str(event_id),
                        run_row["project_id"],
                        json.dumps(payload),
                        processed_at,
                    ),
                )
                events.append(
                    ControlEvent(
                        sequence=event_cursor.lastrowid,
                        event_id=event_id,
                        event_type="research.agent.finished",
                        project_id=run_row["project_id"],
                        payload=payload,
                        created_at=processed_at,
                    )
                )

            checkpoint_id = uuid4()
            self._connection.execute(
                """
                INSERT INTO research_checkpoints(
                    checkpoint_id, run_id, stage, state, completed_agents, created_at
                ) VALUES (?, ?, ?, 'completed', ?, ?)
                """,
                (
                    str(checkpoint_id),
                    run_id,
                    stage,
                    json.dumps(completed_agents),
                    processed_at,
                ),
            )
            checkpoint_event_id = uuid4()
            checkpoint_payload = {
                "run_id": run_id,
                "ticker": analysis.proposal.ticker,
                "stage": stage,
                "status": "completed",
                "completed_agents": completed_agents,
            }
            checkpoint_cursor = self._connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                VALUES (?, 'research.checkpoint.saved', ?, ?, ?)
                """,
                (
                    str(checkpoint_event_id),
                    run_row["project_id"],
                    json.dumps(checkpoint_payload),
                    processed_at,
                ),
            )
            events.append(
                ControlEvent(
                    sequence=checkpoint_cursor.lastrowid,
                    event_id=checkpoint_event_id,
                    event_type="research.checkpoint.saved",
                    project_id=run_row["project_id"],
                    payload=checkpoint_payload,
                    created_at=processed_at,
                )
            )

            completed = stage == len(RESEARCH_STAGES) - 1
            if completed:
                proposal_values = analysis.proposal.model_dump(mode="json")
                self._connection.execute(
                    """
                    INSERT INTO research_proposals(
                        proposal_id, project_id, ticker, analysis_date, rating,
                        suggested_paper_action, report, source, human_approval_required,
                        approval_status, order_created, decided_at, decided_by,
                        decision_reason, trade_id, created_at
                    ) VALUES (
                        :proposal_id, :project_id, :ticker, :analysis_date, :rating,
                        :suggested_paper_action, :report, :source, :human_approval_required,
                        :approval_status, :order_created, :decided_at, :decided_by,
                        :decision_reason, :trade_id, :created_at
                    )
                    """,
                    proposal_values,
                )
                usage_rows = self._connection.execute(
                    """
                    SELECT prompt_tokens, completion_tokens, estimated_cost
                    FROM model_usage WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchall()
                prompt_tokens = sum(row["prompt_tokens"] for row in usage_rows)
                completion_tokens = sum(row["completion_tokens"] for row in usage_rows)
                estimated_cost = sum(
                    (Decimal(row["estimated_cost"]) for row in usage_rows),
                    Decimal(0),
                )
                self._connection.execute(
                    """
                    UPDATE research_runs
                    SET status = 'completed', final_rating = ?, proposal_id = ?,
                        prompt_tokens = ?, completion_tokens = ?, estimated_cost = ?,
                        updated_at = ?, completed_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        analysis.proposal.rating,
                        str(analysis.proposal.proposal_id),
                        prompt_tokens,
                        completion_tokens,
                        str(estimated_cost),
                        processed_at,
                        processed_at,
                        run_id,
                    ),
                )
                self._connection.execute(
                    "DELETE FROM research_analysis_snapshots WHERE run_id = ?", (run_id,)
                )
                event_id = uuid4()
                payload = {
                    "run_id": run_id,
                    "ticker": analysis.proposal.ticker,
                    "status": "completed",
                    "rating": analysis.proposal.rating,
                    "proposal_id": str(analysis.proposal.proposal_id),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "estimated_cost": str(estimated_cost),
                }
                event_cursor = self._connection.execute(
                    """
                    INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                    VALUES (?, 'research.run.completed', ?, ?, ?)
                    """,
                    (
                        str(event_id),
                        run_row["project_id"],
                        json.dumps(payload),
                        processed_at,
                    ),
                )
                events.append(
                    ControlEvent(
                        sequence=event_cursor.lastrowid,
                        event_id=event_id,
                        event_type="research.run.completed",
                        project_id=run_row["project_id"],
                        payload=payload,
                        created_at=processed_at,
                    )
                )
            else:
                self._connection.execute(
                    """
                    UPDATE research_runs
                    SET current_stage = ?, updated_at = ? WHERE run_id = ?
                    """,
                    (stage + 1, processed_at, run_id),
                )

        detail = self.get_research_run_detail(run_id)
        if detail is None:
            raise KeyError(run_id)
        return detail, events, completed

    def complete_research_run(
        self,
        run_id: str,
        analysis: TradingAnalysisResult,
    ) -> tuple[ResearchRunDetail, list[ControlEvent]]:
        completed_at = _now()
        proposal = analysis.proposal
        proposal_values = proposal.model_dump(mode="json")
        events: list[ControlEvent] = []

        with self._lock, self._connection:
            run_row = self._connection.execute(
                "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            if run_row["status"] != "running":
                raise InvalidResearchRun(f"research run is already {run_row['status']}")

            self._connection.execute(
                """
                INSERT INTO research_proposals(
                    proposal_id, project_id, ticker, analysis_date, rating,
                    suggested_paper_action, report, source, human_approval_required,
                    approval_status, order_created, decided_at, decided_by,
                    decision_reason, trade_id, created_at
                ) VALUES (
                    :proposal_id, :project_id, :ticker, :analysis_date, :rating,
                    :suggested_paper_action, :report, :source, :human_approval_required,
                    :approval_status, :order_created, :decided_at, :decided_by,
                    :decision_reason, :trade_id, :created_at
                )
                """,
                proposal_values,
            )

            task_rows = self._connection.execute(
                """
                SELECT * FROM research_agent_tasks
                WHERE run_id = ? ORDER BY stage, agent_id
                """,
                (run_id,),
            ).fetchall()
            for task in task_rows:
                output = analysis.agent_outputs.get(task["agent_id"])
                content = output.content.strip() if output else ""
                task_status = "completed" if content else "skipped"
                summary = (
                    " ".join(content.split())[:500]
                    if content
                    else "No structured output returned by the research engine."
                )
                self._connection.execute(
                    """
                    UPDATE research_agent_tasks
                    SET status = ?, summary = ?, started_at = ?, completed_at = ?
                    WHERE task_id = ?
                    """,
                    (task_status, summary, completed_at, completed_at, task["task_id"]),
                )

                entry_id: str | None = None
                if content:
                    entry_id = str(uuid4())
                    entry_payload = {
                        "stage": task["stage"],
                        "source": proposal.source,
                    }
                    self._connection.execute(
                        """
                        INSERT INTO blackboard_entries(
                            entry_id, run_id, agent_id, topic, content, payload, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entry_id,
                            run_id,
                            task["agent_id"],
                            f"{proposal.ticker} research",
                            content,
                            json.dumps(entry_payload),
                            completed_at,
                        ),
                    )

                event_id = uuid4()
                payload = {
                    "run_id": run_id,
                    "ticker": proposal.ticker,
                    "agent_id": task["agent_id"],
                    "stage": task["stage"],
                    "status": task_status,
                    "blackboard_entry_id": entry_id,
                }
                event_cursor = self._connection.execute(
                    """
                    INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                    VALUES (?, 'research.agent.finished', ?, ?, ?)
                    """,
                    (
                        str(event_id),
                        run_row["project_id"],
                        json.dumps(payload),
                        completed_at,
                    ),
                )
                events.append(
                    ControlEvent(
                        sequence=event_cursor.lastrowid,
                        event_id=event_id,
                        event_type="research.agent.finished",
                        project_id=run_row["project_id"],
                        payload=payload,
                        created_at=completed_at,
                    )
                )

            self._connection.execute(
                """
                UPDATE research_runs
                SET status = 'completed', current_stage = ?, final_rating = ?,
                    proposal_id = ?, updated_at = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (
                    len(RESEARCH_STAGES) - 1,
                    proposal.rating,
                    str(proposal.proposal_id),
                    completed_at,
                    completed_at,
                    run_id,
                ),
            )
            event_id = uuid4()
            payload = {
                "run_id": run_id,
                "ticker": proposal.ticker,
                "status": "completed",
                "rating": proposal.rating,
                "proposal_id": str(proposal.proposal_id),
            }
            event_cursor = self._connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                VALUES (?, 'research.run.completed', ?, ?, ?)
                """,
                (
                    str(event_id),
                    run_row["project_id"],
                    json.dumps(payload),
                    completed_at,
                ),
            )
            events.append(
                ControlEvent(
                    sequence=event_cursor.lastrowid,
                    event_id=event_id,
                    event_type="research.run.completed",
                    project_id=run_row["project_id"],
                    payload=payload,
                    created_at=completed_at,
                )
            )

        detail = self.get_research_run_detail(run_id)
        if detail is None:
            raise KeyError(run_id)
        return detail, events

    def fail_research_run(
        self,
        run_id: str,
        error: str,
    ) -> tuple[ResearchRunDetail, ControlEvent]:
        failed_at = _now()
        event_id = uuid4()
        safe_error = error[:500]

        with self._lock, self._connection:
            run_row = self._connection.execute(
                "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            if run_row["status"] not in {"running", "paused"}:
                raise InvalidResearchRun(f"research run is already {run_row['status']}")
            self._connection.execute(
                """
                UPDATE research_runs
                SET status = 'failed', error = ?, updated_at = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (safe_error, failed_at, failed_at, run_id),
            )
            self._connection.execute(
                """
                UPDATE research_agent_tasks
                SET status = 'failed', summary = ?, completed_at = ?
                WHERE run_id = ? AND status = 'queued'
                """,
                (safe_error, failed_at, run_id),
            )
            payload = {
                "run_id": run_id,
                "ticker": run_row["ticker"],
                "status": "failed",
                "error": safe_error,
            }
            cursor = self._connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                VALUES (?, 'research.run.failed', ?, ?, ?)
                """,
                (
                    str(event_id),
                    run_row["project_id"],
                    json.dumps(payload),
                    failed_at,
                ),
            )

        detail = self.get_research_run_detail(run_id)
        if detail is None:
            raise KeyError(run_id)
        event = ControlEvent(
            sequence=cursor.lastrowid,
            event_id=event_id,
            event_type="research.run.failed",
            project_id=run_row["project_id"],
            payload=payload,
            created_at=failed_at,
        )
        return detail, event

    def pause_research_run(
        self, run_id: str
    ) -> tuple[ResearchRunDetail, ControlEvent]:
        paused_at = _now()
        event_id = uuid4()
        checkpoint_id = uuid4()
        with self._lock, self._connection:
            run_row = self._connection.execute(
                "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            if run_row["status"] != "running":
                raise InvalidResearchRun(f"cannot pause research run from {run_row['status']}")
            completed_agents = [
                row["agent_id"]
                for row in self._connection.execute(
                    """
                    SELECT agent_id FROM research_agent_tasks
                    WHERE run_id = ? AND status IN ('completed', 'skipped')
                    ORDER BY stage, agent_id
                    """,
                    (run_id,),
                ).fetchall()
            ]
            self._connection.execute(
                """
                UPDATE research_runs
                SET status = 'paused', updated_at = ? WHERE run_id = ?
                """,
                (paused_at, run_id),
            )
            self._connection.execute(
                """
                INSERT INTO research_checkpoints(
                    checkpoint_id, run_id, stage, state, completed_agents, created_at
                ) VALUES (?, ?, ?, 'paused', ?, ?)
                """,
                (
                    str(checkpoint_id),
                    run_id,
                    run_row["current_stage"],
                    json.dumps(completed_agents),
                    paused_at,
                ),
            )
            payload = {
                "run_id": run_id,
                "ticker": run_row["ticker"],
                "status": "paused",
                "stage": run_row["current_stage"],
            }
            cursor = self._connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                VALUES (?, 'research.run.paused', ?, ?, ?)
                """,
                (
                    str(event_id),
                    run_row["project_id"],
                    json.dumps(payload),
                    paused_at,
                ),
            )
        detail = self.get_research_run_detail(run_id)
        if detail is None:
            raise KeyError(run_id)
        return detail, ControlEvent(
            sequence=cursor.lastrowid,
            event_id=event_id,
            event_type="research.run.paused",
            project_id=run_row["project_id"],
            payload=payload,
            created_at=paused_at,
        )

    def resume_research_run(
        self, run_id: str
    ) -> tuple[ResearchRunDetail, ControlEvent]:
        resumed_at = _now()
        event_id = uuid4()
        with self._lock, self._connection:
            run_row = self._connection.execute(
                "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            if run_row["status"] != "paused":
                raise InvalidResearchRun(f"cannot resume research run from {run_row['status']}")
            project = self._connection.execute(
                "SELECT state FROM projects WHERE project_id = ?",
                (run_row["project_id"],),
            ).fetchone()
            if project is None or project["state"] != "running":
                raise InvalidResearchRun("project must be running to resume a research run")
            self._connection.execute(
                """
                UPDATE research_runs
                SET status = 'running', updated_at = ? WHERE run_id = ?
                """,
                (resumed_at, run_id),
            )
            payload = {
                "run_id": run_id,
                "ticker": run_row["ticker"],
                "status": "running",
                "stage": run_row["current_stage"],
            }
            cursor = self._connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                VALUES (?, 'research.run.resumed', ?, ?, ?)
                """,
                (
                    str(event_id),
                    run_row["project_id"],
                    json.dumps(payload),
                    resumed_at,
                ),
            )
        detail = self.get_research_run_detail(run_id)
        if detail is None:
            raise KeyError(run_id)
        return detail, ControlEvent(
            sequence=cursor.lastrowid,
            event_id=event_id,
            event_type="research.run.resumed",
            project_id=run_row["project_id"],
            payload=payload,
            created_at=resumed_at,
        )

    def retry_research_run(
        self, run_id: str
    ) -> tuple[ResearchRunDetail, ControlEvent]:
        retried_at = _now()
        event_id = uuid4()
        with self._lock, self._connection:
            run_row = self._connection.execute(
                "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            if run_row["status"] != "failed":
                raise InvalidResearchRun(f"cannot retry research run from {run_row['status']}")
            project = self._connection.execute(
                "SELECT state FROM projects WHERE project_id = ?",
                (run_row["project_id"],),
            ).fetchone()
            if project is None or project["state"] != "running":
                raise InvalidResearchRun("project must be running to retry a research run")
            pending_stage = self._connection.execute(
                """
                SELECT MIN(stage) FROM research_agent_tasks
                WHERE run_id = ? AND status NOT IN ('completed', 'skipped')
                """,
                (run_id,),
            ).fetchone()[0]
            if pending_stage is None:
                pending_stage = run_row["current_stage"]
            next_attempt = run_row["attempt"] + 1
            self._connection.execute(
                """
                UPDATE research_runs
                SET status = 'running', current_stage = ?, attempt = ?, error = NULL,
                    updated_at = ?, completed_at = NULL
                WHERE run_id = ?
                """,
                (pending_stage, next_attempt, retried_at, run_id),
            )
            self._connection.execute(
                """
                UPDATE research_agent_tasks
                SET status = 'queued', summary = '', attempt = ?, confidence = NULL,
                    evidence_ids = '[]', started_at = NULL, completed_at = NULL
                WHERE run_id = ? AND status = 'failed'
                """,
                (next_attempt, run_id),
            )
            payload = {
                "run_id": run_id,
                "ticker": run_row["ticker"],
                "status": "running",
                "stage": pending_stage,
                "attempt": next_attempt,
            }
            cursor = self._connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                VALUES (?, 'research.run.retried', ?, ?, ?)
                """,
                (
                    str(event_id),
                    run_row["project_id"],
                    json.dumps(payload),
                    retried_at,
                ),
            )
        detail = self.get_research_run_detail(run_id)
        if detail is None:
            raise KeyError(run_id)
        return detail, ControlEvent(
            sequence=cursor.lastrowid,
            event_id=event_id,
            event_type="research.run.retried",
            project_id=run_row["project_id"],
            payload=payload,
            created_at=retried_at,
        )

    def pause_active_research_runs(self, project_id: str) -> list[ControlEvent]:
        with self._lock:
            run_ids = [
                row["run_id"]
                for row in self._connection.execute(
                    """
                    SELECT run_id FROM research_runs
                    WHERE project_id = ? AND status = 'running'
                    """,
                    (project_id,),
                ).fetchall()
            ]
        events: list[ControlEvent] = []
        for run_id in run_ids:
            _, event = self.pause_research_run(run_id)
            events.append(event)
        return events

    def recover_interrupted_research_runs(self) -> int:
        with self._lock:
            run_ids = [
                row["run_id"]
                for row in self._connection.execute(
                    "SELECT run_id FROM research_runs WHERE status = 'running'"
                ).fetchall()
            ]
        for run_id in run_ids:
            self.fail_research_run(run_id, "research run interrupted by service restart")
        return len(run_ids)

    def fail_active_research_runs(
        self,
        project_id: str,
        reason: str,
    ) -> list[ControlEvent]:
        with self._lock:
            run_ids = [
                row["run_id"]
                for row in self._connection.execute(
                    """
                    SELECT run_id FROM research_runs
                    WHERE project_id = ? AND status IN ('running', 'paused')
                    """,
                    (project_id,),
                ).fetchall()
            ]
        events: list[ControlEvent] = []
        for run_id in run_ids:
            _, event = self.fail_research_run(run_id, reason)
            events.append(event)
        return events

    def save_research_proposal(self, proposal: TradingProposal) -> TradingProposal:
        values = proposal.model_dump(mode="json")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO research_proposals(
                    proposal_id, project_id, ticker, analysis_date, rating,
                    suggested_paper_action, report, source, human_approval_required,
                    approval_status, order_created, decided_at, decided_by,
                    decision_reason, trade_id, created_at
                ) VALUES (
                    :proposal_id, :project_id, :ticker, :analysis_date, :rating,
                    :suggested_paper_action, :report, :source, :human_approval_required,
                    :approval_status, :order_created, :decided_at, :decided_by,
                    :decision_reason, :trade_id, :created_at
                )
                """,
                values,
            )
        return proposal

    def get_research_proposal(self, proposal_id: str) -> TradingProposal | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM research_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return TradingProposal.model_validate(dict(row)) if row else None

    def decide_research_proposal(
        self,
        proposal_id: str,
        *,
        status: str,
        actor: str,
        reason: str | None,
        trade_id: str | None = None,
    ) -> tuple[TradingProposal, ControlEvent]:
        decided_at = _now()
        event_id = uuid4()
        order_created = trade_id is not None

        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE research_proposals
                SET approval_status = ?, order_created = ?, decided_at = ?, decided_by = ?,
                    decision_reason = ?, trade_id = ?
                WHERE proposal_id = ? AND approval_status = 'pending'
                """,
                (
                    status,
                    order_created,
                    decided_at,
                    actor,
                    reason,
                    trade_id,
                    proposal_id,
                ),
            )
            if cursor.rowcount != 1:
                row = self._connection.execute(
                    "SELECT approval_status FROM research_proposals WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(proposal_id)
                raise InvalidProposalDecision(
                    f"proposal has already been {row['approval_status']}"
                )

            row = self._connection.execute(
                "SELECT * FROM research_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            payload = {
                "proposal_id": proposal_id,
                "ticker": row["ticker"],
                "approval_status": status,
                "order_created": order_created,
                "trade_id": trade_id,
                "actor": actor,
                "reason": reason,
            }
            event_cursor = self._connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(event_id),
                    f"research.proposal.{status}",
                    row["project_id"],
                    json.dumps(payload),
                    decided_at,
                ),
            )

        proposal = TradingProposal.model_validate(dict(row))
        event = ControlEvent(
            sequence=event_cursor.lastrowid,
            event_id=event_id,
            event_type=f"research.proposal.{status}",
            project_id=proposal.project_id,
            payload=payload,
            created_at=decided_at,
        )
        return proposal, event

    def list_research_proposals(self, limit: int = 30) -> list[TradingProposal]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM research_proposals ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [TradingProposal.model_validate(dict(row)) for row in rows]


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[ControlEvent]] = set()

    def subscribe(self) -> asyncio.Queue[ControlEvent]:
        queue: asyncio.Queue[ControlEvent] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ControlEvent]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: ControlEvent) -> None:
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)


class ProjectControl:
    def __init__(self, store: ProjectStore, paper: PaperStore) -> None:
        self.store = store
        self.paper = paper
        self.events = EventHub()
        self._proposal_decision_lock = asyncio.Lock()

    def start_research_run(
        self,
        ticker: str,
        analysis_date: date,
    ) -> ResearchRun:
        run, event = self.store.create_research_run(ticker, analysis_date)
        self.events.publish(event)
        return run

    async def execute_research_run(self, run_id: str) -> None:
        run = self.store.get_research_run(run_id)
        if run is None or run.status != "running":
            return
        try:
            analysis = self.store.get_research_analysis_snapshot(run_id)
            if analysis is None:
                analysis = await asyncio.to_thread(
                    run_trading_agents_analysis,
                    run.ticker,
                    run.analysis_date,
                )
                self.store.save_research_analysis_snapshot(run_id, analysis)
            current_run = self.store.get_research_run(run_id)
            if current_run is None or current_run.status in {"paused", "failed"}:
                return
            project = self.store.get_project(current_run.project_id)
            if project is None or project.state != "running":
                _, event = self.store.fail_research_run(
                    run_id, "research run interrupted by project control"
                )
                self.events.publish(event)
                return
            while True:
                current_run = self.store.get_research_run(run_id)
                if current_run is None or current_run.status != "running":
                    return
                _, events, completed = self.store.process_next_research_stage(run_id)
                for event in events:
                    self.events.publish(event)
                if completed:
                    return
                await asyncio.sleep(0)
        except TradingAgentsUnavailable as error:
            current_run = self.store.get_research_run(run_id)
            if current_run and current_run.status == "running":
                _, event = self.store.fail_research_run(run_id, str(error))
                self.events.publish(event)
        except Exception:  # noqa: BLE001 - background failures must become durable run state
            current_run = self.store.get_research_run(run_id)
            if current_run and current_run.status == "running":
                _, event = self.store.fail_research_run(
                    run_id, "TradingAgents analysis failed"
                )
                self.events.publish(event)

    def pause_research_run(self, run_id: str) -> ResearchRunDetail:
        detail, event = self.store.pause_research_run(run_id)
        self.events.publish(event)
        return detail

    def resume_research_run(self, run_id: str) -> ResearchRunDetail:
        detail, event = self.store.resume_research_run(run_id)
        self.events.publish(event)
        return detail

    def retry_research_run(self, run_id: str) -> ResearchRunDetail:
        detail, event = self.store.retry_research_run(run_id)
        self.events.publish(event)
        return detail

    async def command(
        self,
        project_id: str,
        command: ProjectCommandName,
        actor: str,
        reason: str | None,
    ) -> CommandResult:
        result = self.store.apply_command(project_id, command, actor, reason)
        self.events.publish(result.event)
        if result.project.state == "paused":
            run_events = self.store.pause_active_research_runs(project_id)
            for event in run_events:
                self.events.publish(event)
        elif result.project.state != "running":
            run_events = self.store.fail_active_research_runs(
                project_id,
                f"research run interrupted by project state {result.project.state}",
            )
            for event in run_events:
                self.events.publish(event)
        return result

    async def paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        result = self.paper.submit(order)
        self.events.publish(result.event)
        return result

    async def decide_proposal(
        self,
        proposal_id: str,
        *,
        approve: bool,
        actor: str,
        reason: str | None,
        quantity: int | None = None,
        price: Decimal | None = None,
    ) -> tuple[TradingProposal, PaperOrderResult | None, ControlEvent]:
        async with self._proposal_decision_lock:
            proposal = self.store.get_research_proposal(proposal_id)
            if proposal is None:
                raise KeyError(proposal_id)
            if proposal.approval_status != "pending":
                raise InvalidProposalDecision(
                    f"proposal has already been {proposal.approval_status}"
                )

            order_result: PaperOrderResult | None = None
            if approve and proposal.suggested_paper_action != "hold":
                if quantity is None or price is None:
                    raise InvalidProposalDecision(
                        "quantity and price are required to approve a buy or sell proposal"
                    )
                order_result = self.paper.submit(
                    PaperOrderRequest(
                        project_id=proposal.project_id,
                        ticker=proposal.ticker,
                        side=proposal.suggested_paper_action,
                        quantity=quantity,
                        price=price,
                        reason=reason or f"approved Agent proposal {proposal.proposal_id}",
                    )
                )

            decided, event = self.store.decide_research_proposal(
                proposal_id,
                status="approved" if approve else "rejected",
                actor=actor,
                reason=reason,
                trade_id=str(order_result.trade.trade_id) if order_result else None,
            )
            if order_result:
                self.events.publish(order_result.event)
            self.events.publish(event)
            return decided, order_result, event
