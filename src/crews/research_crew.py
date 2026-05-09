"""Research crew assembly for weekend exploration and backtesting."""
from __future__ import annotations

import logging

from crewai import Crew, Process

from src.agents.research_agents import make_research_manager
from src.tasks.research_tasks import make_research_tasks

log = logging.getLogger(__name__)


class ResearchCrew:
    """Assemble and run the non-trading research crew for a single symbol."""

    def __init__(self, symbol: str = "MGC", verbose: bool = True) -> None:
        self.symbol = symbol.upper()
        self.verbose = verbose
        self._tasks, self._agents = make_research_tasks(symbol=self.symbol)
        self._manager = make_research_manager()

    def kickoff(self, inputs: dict | None = None) -> object:
        if inputs is None:
            inputs = {"symbol": self.symbol}
        else:
            inputs.setdefault("symbol", self.symbol)

        log.info("research_crew.kickoff", extra={"symbol": self.symbol, "inputs": inputs})

        crew = Crew(
            agents=list(self._agents.values()),
            tasks=self._tasks,
            manager_agent=self._manager,
            process=Process.hierarchical,
            verbose=self.verbose,
        )
        result = crew.kickoff(inputs=inputs)
        log.info("research_crew.complete", extra={"symbol": self.symbol})
        return result
