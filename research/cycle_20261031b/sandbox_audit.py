"""SANDBOX BUILTIN AUDIT -- which registered custom strategies can never fire?

The custom-strategy sandbox exposes only ALLOWED_BUILTINS (src/research/
custom_strategy_runtime.py).  Names that are NOT in that set -- getattr,
hasattr, type, Exception, callable, repr, ... -- raise NameError inside
evaluate().  The engine swallows that exception (`except Exception: sig =
None`), so the strategy returns NO SIGNALS AT ALL, silently, with a
"promotion_grade: true" envelope and n_signals = 0.

This script lists every registered strategy whose code references a builtin
the sandbox does not provide, and cross-references the ACTIVE rows.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ALLOWED = {
    "True", "False", "None", "abs", "all", "any", "len", "max", "min", "sum",
    "round", "range", "enumerate", "zip", "list", "dict", "tuple", "set",
    "frozenset", "float", "int", "str", "bool", "isinstance", "sorted",
    "reversed", "map", "filter", "next", "iter", "print", "__build_class__",
    "__name__", "__import__",
}

MISSING = [
    "getattr", "hasattr", "setattr", "delattr", "type", "object", "Exception",
    "BaseException", "callable", "repr", "id", "super", "format", "slice",
    "ord", "chr", "pow", "divmod", "hex", "oct", "bin", "hash", "open", "eval",
    "exec", "compile", "globals", "locals", "vars", "dir", "input", "property",
    "staticmethod", "classmethod", "memoryview", "bytes", "bytearray",
    "complex", "Ellipsis", "NotImplemented", "StopIteration", "KeyboardInterrupt",
    "AssertionError", "ValueError", "TypeError", "KeyError", "IndexError",
    "ZeroDivisionError", "RuntimeError", "OSError", "ImportError",
]

# `except X:` only explodes when an exception actually happens -> fatal only
# on the error path.  Direct calls (`type(x)`, `getattr(x, 'y')`) are fatal
# on EVERY call.
FATAL_CALLS = {"getattr", "hasattr", "setattr", "delattr", "type", "callable",
               "repr", "id", "super", "format", "slice", "ord", "chr", "pow",
               "divmod", "hex", "oct", "bin", "hash", "open", "eval", "exec",
               "compile", "globals", "locals", "vars", "dir", "input",
               "property", "staticmethod", "classmethod", "memoryview",
               "bytes", "bytearray", "complex"}


def scan(code: str) -> dict:
    hits = {}
    for name in MISSING:
        if name in ALLOWED:
            continue
        # direct call  name(
        if re.search(r"(?<![\w.])" + re.escape(name) + r"\s*\(", code):
            hits.setdefault(name, []).append("call")
        # except Name:  /  raise Name(
        if re.search(r"except\s+" + re.escape(name) + r"\b", code):
            hits.setdefault(name, []).append("except")
    return hits


def main():
    conn = sqlite3.connect(ROOT / "judas_crew.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, symbol, code FROM custom_strategies "
        "WHERE code IS NOT NULL AND code != ''").fetchall()
    active = conn.execute(
        "SELECT id, symbol, strategy_family, version, params_json, state "
        "FROM active_strategies WHERE state='active'").fetchall()
    csid_of = {}
    for a in active:
        try:
            pj = json.loads(a["params_json"] or "{}")
        except Exception:
            pj = {}
        cs = pj.get("custom_strategy_id")
        if cs:
            csid_of.setdefault(int(cs), []).append(
                (a["id"], a["symbol"], a["strategy_family"], a["version"]))
    bad = []
    for r in rows:
        code = r["code"]
        if isinstance(code, (bytes, bytearray)):
            code = code.decode("utf-8", "replace")
        h = scan(code)
        if h:
            bad.append((r["id"], r["name"], r["symbol"], h))
    print(f"registered strategies with code: {len(rows)}")
    print(f"strategies referencing a NON-SANDBOX builtin: {len(bad)}")
    for cid, name, sym, h in bad:
        acts = csid_of.get(cid, [])
        tag = "ACTIVE: " + ", ".join(f"#{i} {s} {f} v{v}" for i, s, f, v in acts) if acts else "not active"
        print(f"  csid={cid:<4} {sym:<4} {name[:56]:<56} {h}  {tag}")
    act_bad = [(cid, n, s, h) for cid, n, s, h in bad if cid in csid_of]
    print(f"\nACTIVE strategies pointing at an affected csid: {len(act_bad)}")
    for cid, name, sym, h in act_bad:
        print(f"  csid={cid} {sym} {name} {h} -> actives {csid_of[cid]}")
    (ROOT / "outputs").mkdir(exist_ok=True)
    with open(ROOT / "outputs" / "sandbox_builtin_audit_20261031.json", "w") as fh:
        json.dump([{"csid": c, "name": n, "symbol": s, "hits": h,
                    "actives": csid_of.get(c, [])} for c, n, s, h in bad],
                  fh, indent=1, default=str)


if __name__ == "__main__":
    main()
