# MVP 2B Checkpointed Research, Agent Approval, and Paper Trading API

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
| `POST` | `/research/proposals` | Run TradingAgents and save a pending proposal |
| `GET` | `/research/proposals` | List durable Agent proposals |
| `GET` | `/research/proposals/{proposal_id}` | Read one durable Agent proposal |
| `POST` | `/research/proposals/{proposal_id}/approve` | Approve and optionally create its Paper order |
| `POST` | `/research/proposals/{proposal_id}/reject` | Reject without creating an order |
| `POST` | `/research/runs` | Start a durable multi-Agent research run |
| `GET` | `/research/runs` | List durable research runs |
| `GET` | `/research/runs/{run_id}` | Read run, Agent tasks, and Blackboard entries |
| `POST` | `/research/runs/{run_id}/pause` | Save a paused checkpoint |
| `POST` | `/research/runs/{run_id}/resume` | Resume from the next incomplete stage |
| `POST` | `/research/runs/{run_id}/retry` | Retry a failed run and increment its attempt |
| `GET` | `/paper/portfolio` | Paper cash, cost basis, realized P&L, and positions |
| `GET` | `/paper/trades` | Durable paper trade history |
| `POST` | `/paper/orders` | Immediately fill a manually priced paper order |
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

Project pause also pauses active research runs and writes paused checkpoints. Project resume
does not implicitly resume them; each run requires its explicit `/resume` call. Project stop
or kill fails active research runs and discards any late provider result.

## Paper order

The project must be in the `running` state. Orders use whole shares and a caller-provided
price, then fill immediately against the SQLite paper account. The default starting cash is
USD 100,000. Insufficient paper cash or position returns HTTP `409`.

```json
{
  "project_id": "investment-town",
  "ticker": "NVDA",
  "side": "buy",
  "quantity": 10,
  "price": "100.00",
  "reason": "manual dashboard order"
}
```

Each accepted order atomically updates the paper account and position, writes a trade, and
adds a `paper.order.filled` event. `book_value` is cash plus position cost basis because MVP
1.1 has no market-data feed. These endpoints never call a brokerage or transmit a live order.

Deferred beyond MVP 1.1: market prices, fees, taxes, fractional shares, partial fills,
strategy automation, Agent-approved orders, Toss Securities credentials, and live execution.

## Agent research proposal

The optional TradingAgents integration accepts a ticker and analysis date. A successful run
stores a pending proposal containing the five-tier rating, suggested paper action, and final
report. It never submits an order.

```json
{
  "ticker": "NVDA",
  "analysis_date": "2026-08-13"
}
```

The endpoint returns HTTP `503` until the backend `agents` extra is installed and HTTP `502`
when the configured LLM or market-data provider fails. Every saved proposal begins with
`human_approval_required: true`, `approval_status: "pending"`, and `order_created: false`.

### Human decision

A buy or sell approval must include the operator-selected whole-share quantity and Paper
fill price. The project must already be running, and the existing Paper cash/position checks
still apply.

```json
{
  "quantity": 10,
  "price": "125.50",
  "reason": "reviewed the report and risk context"
}
```

An approved `hold` proposal records the decision without creating an order and does not
require quantity or price. Rejection accepts only an optional reason:

```json
{
  "reason": "evidence is insufficient"
}
```

Only a `pending` proposal can be decided. A repeated approval, a later approval after
rejection, missing order terms, an invalid project state, or insufficient Paper balance
returns HTTP `409`. Successful decisions persist the operator, timestamp, reason, optional
trade ID, and a `research.proposal.approved` or `research.proposal.rejected` event.

No endpoint can turn an Agent proposal into a live-broker order.

## Durable research run

`POST /research/runs` accepts the same ticker and analysis date as the legacy direct proposal
endpoint and returns HTTP `202`. The project must be running.

```json
{
  "ticker": "NVDA",
  "analysis_date": "2026-08-19"
}
```

The returned run begins as `running`. Fetch its detail endpoint to inspect checkpoints,
reported usage, eight staged Agent tasks, and any shared Blackboard entries. Successful completion
atomically stores normalized Agent outputs and a linked pending proposal. Missing upstream
Agent fields become `skipped` tasks rather than fabricated content. Provider failure creates
no proposal, and a run interrupted by service restart becomes `failed` on the next startup.

Pause preserves the normalized provider result and current stage. Resume does not call the
provider again when that snapshot exists. Retry increments the run and task attempt, keeps
completed stages, and resets failed tasks. Token and cost totals include only metadata
reported by the provider; missing usage is recorded as unknown rather than estimated.

The direct `POST /research/proposals` endpoint remains for compatibility, but the dashboard
uses durable runs.

## MVP storage boundary

MVP 2B uses SQLite, FastAPI background tasks, in-process run/proposal coordination, and an
in-process WebSocket
fan-out. This is sufficient for one control API process. Move project, event, audit, paper
account, position, and trade records to PostgreSQL with one transactional approval/order
service, run research through durable workers, and move the fan-out to Redis Streams before
running multiple API instances.
