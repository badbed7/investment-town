# TradingAgents reference

Investment Town uses
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) as an
optional research engine reference. It is not part of the default Railway runtime.

- Upstream version: commit `a33fd4c0f134485a43553a2c23a63cb14adbd88f`
- Upstream license: Apache License 2.0
- Integration boundary: `backend/src/investment_town/integrations/trading_agents.py`
- Safety boundary: the adapter returns a proposal only; it never submits an order.

Install the optional dependency from `backend/` when an LLM provider and market-data
configuration are ready:

```bash
pip install -e ".[agents,dev]"
```

TradingAgents ratings map to paper-trading suggestions as follows:

| Rating | Suggested paper action |
|---|---|
| Buy / Overweight | buy |
| Hold | hold |
| Underweight / Sell | sell |

Every suggestion still requires explicit human approval. Live brokerage execution remains
disabled.

## MVP 1.2A flow

1. `POST /api/v1/research/proposals` runs the pinned TradingAgents engine.
2. The adapter maps its five-tier rating to a paper action suggestion.
3. The proposal and report are stored in the existing SQLite volume.
4. `GET /api/v1/research/proposals` restores the latest proposals after restart.
5. No approval or order endpoint exists in MVP 1.2A, so a proposal cannot execute itself.

The Railway image continues to install the base backend only. Enable the optional dependency
and configure an LLM provider key when Agent analysis is ready for trial operation.
