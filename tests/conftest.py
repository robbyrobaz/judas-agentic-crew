"""Shared test fixtures for judas-agentic-crew."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `import src...` works from tests/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def pytest_configure(config):
    # Workshop path resolution defaults to ../judas-futures-workshop relative
    # to the repo root, but the agentic-crew lives at /home/rob/... so we
    # point JUDAS_WORKSHOP_PATH to the canonical workshop checkout for tests.
    workshop = Path("/home/rob/judas-futures-workshop")
    if workshop.exists():
        os.environ.setdefault("JUDAS_WORKSHOP_PATH", str(workshop))

    # Safety defaults for the entire test suite — never reach real M2.7 or
    # the live IBKR paper account. Tests that need the LLM / broker / autofix
    # paths must opt-in via monkeypatch.setenv / delenv.
    #   - MINIMAX_API_KEY popped: pm_agent / live_review / explore enter
    #     deterministic-fallback or no-op modes.
    #   - JUDAS_AUTOFIX_INHIBIT=1: fix_bug_step records symptoms only.
    #   - JUDAS_PM_AGENT_INHIBIT=1: short-circuits run_pm_decision so the
    #     agent loop never spins up under pytest.
    os.environ.pop("MINIMAX_API_KEY", None)
    os.environ.setdefault("JUDAS_AUTOFIX_INHIBIT", "1")
    os.environ.setdefault("JUDAS_PM_AGENT_INHIBIT", "1")
    # Phase 10 specialists inhibited by default — tests opt in by clearing.
    os.environ.setdefault("JUDAS_OPERATOR_AGENT_INHIBIT", "1")
    os.environ.setdefault("JUDAS_RESEARCHER_AGENT_INHIBIT", "1")
    os.environ.setdefault("JUDAS_TRADER_AGENT_INHIBIT", "1")
    os.environ.setdefault("JUDAS_REGISTRAR_AGENT_INHIBIT", "1")
    os.environ.setdefault("JUDAS_CODER_AGENT_INHIBIT", "1")
