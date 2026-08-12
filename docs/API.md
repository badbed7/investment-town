# MVP 1 Control API

Base path: `/api/v1`

Except for health, endpoints require `Authorization: Bearer <CONTROL_API_TOKEN>` when a
token is configured. Local `APP_ENV=development` permits an empty token. Other environments
fail closed if `CONTROL_API_TOKEN` is missing.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Service and paper-only status |
| `GET` | `/projects` | Registered projects |
| `GET` | `/projects/{project_id}` | Current project state |
| `POST` | `/projects/{project_id}/commands/{command}` | Start, pause, resume, stop, or kill |
| `GET` | `/events` | Durable recent events |
| `GET` | `/audit` | Durable command audit entries |
| `WS` | `/events/stream` | Backlog followed by live events |

Command body:

```json
{
  "reason": "manual mobile control"
}
```

When `CONTROL_API_TOKEN` is configured, a WebSocket client must send the following message
within five seconds after connecting:

```json
{
  "token": "..."
}
```

## State transitions

| Command | Allowed source | Result |
|---|---|---|
| `start` | `idle`, `stopped`, `failed` | `running` |
| `pause` | `running` | `paused` |
| `resume` | `paused` | `running` |
| `stop` | `running`, `paused` | `stopped` |
| `kill` | any non-killed state | `killed` |

Invalid transitions return HTTP `409`. Every accepted command atomically updates project
state and writes one event and one audit entry.

## MVP storage boundary

MVP 1 uses SQLite and an in-process WebSocket fan-out. This is sufficient for one control
API process. Move the same project/event/audit records to PostgreSQL and the fan-out to Redis
Streams before running multiple API instances.
