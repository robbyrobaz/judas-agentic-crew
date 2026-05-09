"""Task definitions for the weekend ResearchCrew."""
from __future__ import annotations

from crewai import Task

from src.agents.research_agents import (
    make_research_analyst,
    make_research_archivist,
    make_research_editor,
    make_research_librarian,
)


def make_research_tasks(symbol: str = "MGC") -> tuple[list[Task], dict]:
    librarian = make_research_librarian()
    analyst = make_research_analyst()
    editor = make_research_editor()
    archivist = make_research_archivist()

    task_inventory = Task(
        description=(
            f"Inventory the local judas-futures-workshop assets relevant to {symbol}.\n\n"
            f"Steps:\n"
            f"1. Call workshop_inventory_tool.\n"
            f"2. Call workshop_findings_tool.\n"
            f"3. Call workshop_leaderboard_tool to pull the standing top workshop winners.\n"
            f"4. Return a concise JSON summary of the datasets, cached 1H files, top-ranked "
            f"existing strategy families, and "
            f"existing findings most relevant to 1H Judas research."
        ),
        expected_output=(
            '{'
            '"symbol": "MGC", '
            '"inventory": {...}, '
            '"workshop_top_strategies": [...], '
            '"key_findings": ["..."], '
            '"research_hypotheses": ["..."]'
            '}'
        ),
        agent=librarian,
    )

    task_backtest = Task(
        description=(
            f"Run deterministic research for {symbol} using the workshop data.\n\n"
            f"Steps:\n"
            f"1. Call workshop_fast_battery_tool for {symbol} on 1H cached data.\n"
            f"2. Call workshop_leaderboard_tool so the existing top workshop strategies are treated as the baseline benchmark set.\n"
            f"3. Call judas_backtest_tool for {symbol}.\n"
            f"4. Call judas_parameter_sweep_tool for {symbol} to explore Judas 1H rule variants.\n"
            f"5. Call walk_forward_tool on the most promising Judas parameter set.\n"
            f"6. Compare the broad 1H benchmark results against the Judas-specific results.\n"
            f"7. Return a JSON object with the benchmark summary, leaderboard benchmark set, sweep winners, walk-forward "
            f"summary, and the specific threshold ideas suggested by the data."
        ),
        expected_output=(
            '{'
            '"symbol": "MGC", '
            '"benchmark_summary": {...}, '
            '"benchmark_top_strategies": [...], '
            '"judas_summary": {...}, '
            '"sweep_top_results": [...], '
            '"walk_forward_summary": {...}, '
            '"candidate_action": {...}, '
            '"candidate_filters": ["..."], '
            '"evidence_paths": {"battery_csv": "...", "judas_json": "...", "sweep_json": "...", "walk_forward_json": "..."}'
            '}'
        ),
        context=[task_inventory],
        agent=analyst,
    )

    task_synthesis = Task(
        description=(
            f"Synthesize the research outputs into operational recommendations for {symbol}.\n\n"
            f"Focus on Judas on 1H: sweep quality, displacement thresholds, ATR filters, "
            f"and session timing. Recommend changes, but do not auto-apply anything to the "
            f"live trading crew."
        ),
        expected_output=(
            '{'
            '"title": "MGC Weekend Research", '
            '"summary": "...", '
            '"recommendations": ["..."], '
            '"evidence": {...}'
            '}'
        ),
        context=[task_inventory, task_backtest],
        agent=editor,
    )

    task_archive = Task(
        description=(
            "Save the final research synthesis to outputs/research using save_research_report_tool. "
            "Return the report paths and a short archival confirmation."
        ),
        expected_output=(
            '{'
            '"saved": true, '
            '"json_path": "...", '
            '"md_path": "...", '
            '"message": "..."'
            '}'
        ),
        context=[task_synthesis],
        agent=archivist,
    )

    tasks = [task_inventory, task_backtest, task_synthesis, task_archive]
    agents = {
        "librarian": librarian,
        "analyst": analyst,
        "editor": editor,
        "archivist": archivist,
    }
    return tasks, agents
