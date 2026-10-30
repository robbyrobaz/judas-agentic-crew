"""Apply the production-fill patch to custom_strategy_runtime.py.

Replaces the region between the state-init marker and the `_run_with_timeout`
call with the new fill-aware implementation, and wires fill_mode through the
public entry point + the agent tool.
"""
from __future__ import annotations

from pathlib import Path

SRC = Path("src/research/custom_strategy_runtime.py")
block = Path("research/patches/fill_mode_block.py").read_text()

text = SRC.read_text()

START = "    n_signals = 0\n    wins = 0\n"
END = "        _run_with_timeout(_run_loop, timeout_s)"

i = text.index(START)
j = text.index(END)
assert i < j, "markers out of order"
new_text = text[:i] + block + text[j:]
SRC.write_text(new_text)
print("patched region:", j - i, "->", len(block))
