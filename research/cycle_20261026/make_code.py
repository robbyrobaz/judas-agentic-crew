"""Render a market-entry strategy file with a BAKED-IN configuration.

The sanctioned `run_custom_backtest` tool takes no params, so the promoted
configuration has to live in the code's DEFAULTS.  The live path merges the
active row's params_json over the defaults, so both paths agree.

Usage: .venv/bin/python research/cycle_20261026/make_code.py <name> <json-config>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "research" / "cycle_20261026" / "template_market_entry.py"
OUT_DIR = ROOT / "research" / "cycle_20261026" / "strategies"


def render(name: str, cfg: dict) -> str:
    src = TEMPLATE.read_text()
    start = src.index("DEFAULTS = {")
    end = src.index("}", start) + 1
    body = "DEFAULTS = {\n" + "".join(
        f'    "{k}": {json.dumps(v)},\n' for k, v in cfg.items()
    ) + "}"
    src = src[:start] + body + src[end:]
    src = src.replace(
        '"""MARKET-ENTRY SWEEP -> DISPLACEMENT CONTINUATION (template).',
        f'"""MARKET-ENTRY SWEEP -> DISPLACEMENT CONTINUATION -- {name}.',
    )
    return src


def main() -> int:
    name = sys.argv[1]
    cfg = json.loads(sys.argv[2])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{name}.py"
    out.write_text(render(name, cfg))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
