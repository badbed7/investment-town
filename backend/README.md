# Investment Town Backend

Python is the source-of-truth runtime for Investment Town.

## Current MVP scope

- FastAPI control API
- Agent role registry
- Shared event and command schemas
- Deterministic research workflow outline
- Paper broker placeholder
- Paper-trading-only configuration

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

## Important

No live broker orders are implemented in the MVP. Live execution must remain behind explicit approval and deterministic risk gates.
