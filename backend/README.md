# Investment Town Backend

Python is the source-of-truth runtime for Investment Town.

## Current MVP scope

- FastAPI control API
- Agent role registry
- Shared event and command schemas
- Deterministic research workflow outline
- Paper broker placeholder
- Paper-trading-only configuration
- Durable project registry and command state machine
- WebSocket event stream and audit log
- Mobile-friendly control dashboard
- Durable Agent proposal review queue
- Human approval or rejection before an Agent-suggested Paper order
- Durable multi-Agent research runs, task timeline, and shared Blackboard
- Safe interrupted-run recovery after a service restart
- Stage checkpoints with explicit pause, resume, and retry controls
- Provider-reported evidence, confidence, token usage, and estimated cost logging

## Run locally

From `backend/`:

```bash
python -m venv .venv
```

Activate the virtual environment, then:

```bash
pip install -e .[dev]
uvicorn investment_town.main:app --reload
```

Open `http://127.0.0.1:8000/docs`.

Open `http://127.0.0.1:8000` for the control dashboard. Set `CONTROL_API_TOKEN` before
exposing the server outside local development. Non-development environments fail closed
when the token is missing.

## Important

No live broker orders are implemented in the MVP. Approval can create only a local Paper
order at the quantity and price supplied by the operator. Live execution must remain behind
explicit approval and deterministic risk gates.
