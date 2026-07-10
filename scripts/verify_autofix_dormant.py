"""WS3 verification: autofix #396 merge state + dormant custom-cluster status.

Reports three things:
  1. Whether the autofix/20260709T180146Z-tool-failure branch is merged into HEAD.
  2. Whether auto_fixes row #396 is 'completed' and pushed.
  3. Per-active firing history for all custom-family strategies (active=1, state=active).

Verdict is logged, and a coder-escalation packet is printed when the autofix is not
merged and the dormant cluster is unresolved.

Usage:  python3 scripts/verify_autofix_dormant.py
From any CWD:  python3 /full/path/to/scripts/verify_autofix_dormant.py
"""
import json
import sqlite3
import subprocess
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "judas_crew.db"


def check_git_branch(branch: str) -> dict:
    """Check whether the named branch is merged into HEAD."""
    result = {"branch": branch, "merged": False, "tip": None}
    try:
        subprocess.check_output(
            ["git", "merge-base", "--is-ancestor", branch, "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        result["merged"] = True
    except (subprocess.CalledProcessError, FileNotFoundError):
        result["merged"] = False
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", branch], stderr=subprocess.DEVNULL
        ).decode().strip()
        result["tip"] = out
    except subprocess.CalledProcessError:
        pass
    return result


def check_autofix_db(branch: str) -> dict:
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    cur.execute(
        "SELECT id, branch_name, status, pushed, started_at_utc, finished_at_utc, "
        "test_result FROM auto_fixes WHERE branch_name=?",
        (branch,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"exists": False}
    return {
        "exists": True,
        "id": row[0], "branch_name": row[1], "status": row[2],
        "pushed": bool(row[3]), "started_at_utc": row[4], "finished_at_utc": row[5],
        "test_result": row[6],
    }


def list_dormant_customs() -> list:
    """Custom-family actives + their last-fire timestamp + lifetime trade count."""
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT a.id, a.symbol, a.strategy_family, a.activated_at_utc,
               COALESCE(MAX(t.closed_at), '') AS last_fire,
               (SELECT COUNT(*) FROM trades WHERE strategy_id=a.id) AS lifetime_trades
        FROM active_strategies a
        LEFT JOIN trades t ON t.strategy_id = a.id
        WHERE a.strategy_family LIKE 'custom%' AND a.state='active'
        GROUP BY a.id
        ORDER BY a.id
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def main() -> None:
    branch_name = "autofix/20260709T180146Z-tool-failure"
    print("=" * 78)
    print(f"WS3 VERIFICATION — autofix merge state + dormant-cluster status ({branch_name})")
    print("=" * 78)

    # 1. Branch state
    print("\n[1] Branch state:")
    branch = check_git_branch(branch_name)
    print(json.dumps(branch, indent=2))

    # 2. auto_fixes DB row
    print("\n[2] auto_fixes row:")
    af = check_autofix_db(branch_name)
    print(json.dumps(af, indent=2))

    # 3. Custom actives — dormant vs firing
    print("\n[3] Custom-family ACTIVE strategies (state=active):")
    customs = list_dormant_customs()
    never_fired = [c for c in customs if not c["last_fire"]]
    fired_anytime = [c for c in customs if c["last_fire"]]
    print(f"  Total custom-family actives: {len(customs)}")
    print(f"  Never fired (lifetime_trades=0): {len(never_fired)}")
    for c in never_fired:
        print(
            f"    #{c['id']:4} {c['symbol']:4} {c['strategy_family']:30} "
            f"activated {c['activated_at_utc']} lifetime_trades={c['lifetime_trades']}"
        )
    print(f"\n  Fired at least once: {len(fired_anytime)}")
    for c in fired_anytime:
        print(
            f"    #{c['id']:4} {c['symbol']:4} {c['strategy_family']:30} "
            f"last_fire={c['last_fire']} lifetime_trades={c['lifetime_trades']}"
        )

    # 4. Verdict + escalation
    print("\n[4] VERDICT")
    if not af["exists"]:
        print("  ✗ No auto_fixes row for this branch — escalate to operator.")
    if branch["merged"]:
        print("  ✓ Branch IS merged into HEAD — state-reset fix is live.")
        if never_fired:
            print(f"  ⚠ {len(never_fired)} customs still dormant. Bug NOT in state-reset path.")
            print("    Likely suspects: eval_only_no_orders flag (defect 3), retire pipeline (defect 2),")
            print("    or bar-feed thinness. Run scripts/check_eval_only.py on each.")
    else:
        print(f"  ✗ Branch NOT merged (tip={branch['tip']}). The _STATE_CACHE fix is sitting on")
        print("    the branch but the live runtime re-imports each evaluate() — module-level _S")
        print("    dicts reset between bars and cross-bar setup accumulation never completes.")
        if never_fired:
            print(f"  ✗ {len(never_fired)} customs confirmatively dormant — bug holds them silent.")
            print()
            print("  ── ESCALATION PACKET for coder/manual merge ────────────────────────")
            print("  Procedures (preserves last_bar_closes.json):")
            print("    1. git fetch origin " + branch_name)
            print("    2. git stash push -- last_bar_closes.json")
            print("    3. git merge --no-ff " + branch_name + " -m 'merge: state-reset fix (#396)'")
            print("    4. git stash pop")
            print("    5. .venv/bin/python -m pytest tests/test_custom_strategy_state.py -q")
            print("    6. Re-run python3 scripts/verify_autofix_dormant.py — never_fired should be 0.")
        else:
            print("  ✓ All customs fired — bug does NOT explain the dormant cluster;")
            print("    another root cause is at work.")


if __name__ == "__main__":
    main()
