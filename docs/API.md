# MVP 1.2A Control, Agent Proposal, and Paper Trading API

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
when the configured LLM or market-data provider fails. Every saved proposal has
`human_approval_required: true`, `approval_status: "pending"`, and `order_created: false`.

## MVP storage boundary

MVP 1.2A uses SQLite and an in-process WebSocket fan-out. This is sufficient for one control
API process. Move project, event, audit, paper account, position, and trade records to
PostgreSQL and the fan-out to Redis Streams before running multiple API instances.
