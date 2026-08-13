import asyncio
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from investment_town.broker.paper import PaperOrderRequest, PaperOrderResult, PaperStore
from investment_town.schemas.commands import ProjectCommandName
from investment_town.schemas.control import (
    AuditEntry,
    CommandResult,
    ControlEvent,
    Project,
    ProjectHealth,
    ProjectState,
)

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
                """
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

    async def command(
        self,
        project_id: str,
        command: ProjectCommandName,
        actor: str,
        reason: str | None,
    ) -> CommandResult:
        result = self.store.apply_command(project_id, command, actor, reason)
        self.events.publish(result.event)
        return result

    async def paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        result = self.paper.submit(order)
        self.events.publish(result.event)
        return result
