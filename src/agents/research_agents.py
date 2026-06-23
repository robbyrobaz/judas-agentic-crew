"""Agent definitions for the weekend ResearchCrew."""
from __future__ import annotations

from crewai import Agent, LLM

from src.agents.judas_agents import build_llm
from src.agents.operator_context import build_operator_appendix
from src.config import load_config
from src.tools.research_tools import (
    experiment_catalog_tool,
    experiment_ranker_tool,
    judas_backtest_tool,
    judas_parameter_sweep_tool,
    save_research_report_tool,
    strategy_registry_tool,
    walk_forward_tool,
    workshop_fast_battery_tool,
    workshop_findings_tool,
    workshop_inventory_tool,
    workshop_leaderboard_tool,
)


def _build_research_manager_llm() -> LLM:
    cfg = load_config()
    additional_params: dict[str, object] = {"extra_body": {"reasoning_split": True}}
    return LLM(
        model="minimax/MiniMax-M3",
        api_key=cfg.minimax_api_key,
        api_base=cfg.crew.llm_base_url,
        provider=cfg.crew.llm_provider,
        temperature=0.0,
        max_tokens=4096,
        additional_params=additional_params,
    )


def make_research_manager() -> Agent:
    return Agent(
        role="Research Manager",
        goal=(
            "Coordinate research as a reusable experimentation lab: choose deterministic "
            "sweeps, walk-forward studies, and benchmarks, then produce evidence-backed "
            "improvement proposals without changing the live execution path."
        ),
        backstory=(
            "You are a research lead for a futures trading lab. You manage analysts, "
            "request the right deterministic studies, and push for useful conclusions. "
            "You optimize for robustness, not curve-fit glamour. You may delegate within "
            "the research crew, but you never place trades and never auto-apply research "
            "changes to the live TradingCrew."
        ) + build_operator_appendix(),
        llm=_build_research_manager_llm(),
        allow_delegation=True,
        verbose=True,
        max_iter=8,
    )


def make_research_librarian() -> Agent:
    return Agent(
        role="Workshop Research Librarian",
        goal=(
            "Load the existing workshop findings, cached datasets, and prior battery "
            "artifacts so the rest of the research crew works from known evidence."
        ),
        backstory=(
            "You are a careful research librarian. You do not speculate when local "
            "evidence already exists. You inventory the workshop repo, surface the "
            "relevant findings, and keep the rest of the crew grounded in that data."
        ) + build_operator_appendix(),
        tools=[workshop_inventory_tool, workshop_findings_tool, workshop_leaderboard_tool],
        llm=build_llm(),
        allow_delegation=False,
        verbose=True,
        max_iter=4,
    )


def make_research_analyst() -> Agent:
    return Agent(
        role="Systematic Strategy Research Analyst",
        goal=(
            "Run deterministic backtests and benchmarking over the workshop's cached "
            "1H data, then identify what the data suggests about Judas quality filters, "
            "parameter sweeps, and walk-forward robustness."
        ),
        backstory=(
            "You are a pragmatic quant researcher. You do not guess; you run the "
            "battery and the historical Judas backtest, then compare the results. "
            "Your job is to find useful thresholds and constraints, not to defend a bias."
        ) + build_operator_appendix(),
        tools=[workshop_fast_battery_tool, judas_backtest_tool, judas_parameter_sweep_tool, walk_forward_tool, workshop_leaderboard_tool],
        llm=build_llm(),
        allow_delegation=False,
        verbose=True,
        max_iter=6,
    )


def make_research_editor() -> Agent:
    return Agent(
        role="Trading Research Editor",
        goal=(
            "Synthesize deterministic research outputs into clear, operationally useful "
            "recommendations and ranked proposals for the live trading crew without auto-applying changes."
        ),
        backstory=(
            "You are a disciplined editor. You turn noisy technical evidence into a "
            "small number of concrete recommendations. You never claim a rule is "
            "validated unless the deterministic outputs support it."
        ) + build_operator_appendix(),
        tools=[experiment_ranker_tool, strategy_registry_tool],
        llm=build_llm(),
        allow_delegation=False,
        verbose=True,
        max_iter=4,
    )


def make_research_archivist() -> Agent:
    return Agent(
        role="Research Archivist",
        goal=(
            "Persist the final research report and its evidence to outputs/research so "
            "future sessions and weekend runs can build on it."
        ),
        backstory=(
            "You are an archivist. You save the report faithfully with links to the "
            "deterministic outputs. You do not place trades or alter live config."
        ) + build_operator_appendix(),
        tools=[save_research_report_tool],
        llm=build_llm(),
        allow_delegation=False,
        verbose=True,
        max_iter=3,
    )
