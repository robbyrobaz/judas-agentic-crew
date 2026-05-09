"""Judas Swing CrewAI crew assembly.

Assembles all agents and tasks into a sequential Crew.
Usage:
    from src.crews.judas_crew import JudasCrew
    result = JudasCrew(symbol="MGC").kickoff()
"""
from __future__ import annotations

import logging

from crewai import Crew, Process

from src.tasks.judas_tasks import make_tasks

log = logging.getLogger(__name__)


class JudasCrew:
    """Assembles and runs the Judas Swing trading crew for a single symbol."""

    def __init__(self, symbol: str = "MGC", verbose: bool = True) -> None:
        self.symbol = symbol.upper()
        self.verbose = verbose
        self._tasks, self._agents = make_tasks(symbol=self.symbol)

    def kickoff(self, inputs: dict | None = None) -> object:
        """Run the crew synchronously and return the CrewOutput.

        Args:
            inputs: Optional dict of template variables to pass to tasks.
                    At minimum should contain {"symbol": "MGC"}.

        Returns:
            CrewOutput from crewai.Crew.kickoff().
        """
        if inputs is None:
            inputs = {"symbol": self.symbol}
        else:
            inputs.setdefault("symbol", self.symbol)

        log.info("judas_crew.kickoff", extra={"symbol": self.symbol, "inputs": inputs})

        crew = Crew(
            agents=list(self._agents.values()),
            tasks=self._tasks,
            process=Process.sequential,
            verbose=self.verbose,
        )

        try:
            result = crew.kickoff(inputs=inputs)
            log.info("judas_crew.complete", extra={"symbol": self.symbol})
            return result
        except Exception as e:
            log.error(
                "judas_crew.error",
                extra={"symbol": self.symbol, "error": str(e)},
                exc_info=True,
            )
            raise
