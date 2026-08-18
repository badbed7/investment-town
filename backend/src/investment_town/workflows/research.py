from investment_town.agents.registry import AGENT_ROLES

RESEARCH_STAGES: tuple[tuple[str, ...], ...] = (
    ("news", "fundamental", "macro", "quant"),
    ("bull", "bear"),
    ("risk",),
    ("portfolio_manager",),
)


def research_workflow_outline(ticker: str) -> dict[str, object]:
    """Return the deterministic MVP workflow plan before LLM wiring is added."""
    return {
        "ticker": ticker.upper(),
        "stages": [list(stage) for stage in RESEARCH_STAGES],
        "registered_agents": list(AGENT_ROLES),
        "execution_mode": "paper",
    }
