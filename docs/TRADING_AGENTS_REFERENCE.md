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

## MVP 2A flow

1. `POST /api/v1/research/runs` creates a durable run and staged Agent tasks.
2. A background task runs the pinned TradingAgents engine.
3. The adapter maps upstream state to Investment Town Agent roles.
4. Real outputs become shared Blackboard entries; missing fields become skipped tasks.
5. The adapter maps the final five-tier rating to a paper action suggestion.
6. Run completion and the pending proposal are stored in the existing SQLite volume.
7. A user reviews the proposal in the control dashboard and explicitly approves or rejects it.
8. Buy and sell approvals require a user-entered quantity and Paper price.
9. The existing Paper broker validates project state, cash, and positions before filling.
10. A proposal still cannot execute itself or reach a live broker.

The Railway image continues to install the base backend only. Enable the optional dependency
and configure an LLM provider key when Agent analysis is ready for trial operation.
