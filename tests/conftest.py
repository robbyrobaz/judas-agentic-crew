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
