from investment_town.agents.registry import AGENT_ROLES


def research_workflow_outline(ticker: str) -> dict[str, object]:
    """Return the deterministic MVP workflow plan before LLM wiring is added."""
    return {
        "ticker": ticker.upper(),
        "stages": [
            ["news", "fundamental", "macro", "quant"],
            ["bull", "bear"],
            ["risk"],
            ["portfolio_manager"],
        ],
        "registered_agents": list(AGENT_ROLES),
        "execution_mode": "paper",
    }
