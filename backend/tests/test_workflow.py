from investment_town.workflows.research import research_workflow_outline


def test_research_workflow_is_paper_only() -> None:
    plan = research_workflow_outline("nvda")
    assert plan["ticker"] == "NVDA"
    assert plan["execution_mode"] == "paper"
    assert plan["stages"][-1] == ["portfolio_manager"]
