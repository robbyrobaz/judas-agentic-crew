"""Phase 9 tests: web/youtube/fs/sandbox + custom-strategy dispatch.

Network is never reached. We patch ``ddgs.DDGS``, ``requests.get``, and
``youtube_transcript_api.YouTubeTranscriptApi`` at the module-import sites
inside the pm_agent tools.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.models import init_db, get_conn  # noqa: E402
from src.research import pm_agent  # noqa: E402
from src.research import custom_strategy_runtime as csr  # noqa: E402


# Conftest sets JUDAS_PM_AGENT_INHIBIT=1; tools work directly so no need to
# clear, but be safe on import-paths.
@pytest.fixture(autouse=True)
def _enable_for_tests(monkeypatch, tmp_path):
    monkeypatch.delenv("JUDAS_PM_AGENT_INHIBIT", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "fake")


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "phase9.db")
    init_db(p)
    return p


@pytest.fixture
def tools(db):
    return pm_agent._make_tools(db_path=db)


# ---------------------------------------------------------------------------
# Web tools
# ---------------------------------------------------------------------------


class _FakeDDGS:
    _payload: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def text(self, query, max_results=5):
        return list(self._payload)[:max_results]


def test_web_search_caps_snippets(tools, monkeypatch):
    long_snip = "x" * 500
    _FakeDDGS._payload = [
        {"title": "T1", "href": "http://a", "body": long_snip},
        {"title": "T2", "href": "http://b", "body": "short"},
    ]
    import ddgs as ddgs_mod
    monkeypatch.setattr(ddgs_mod, "DDGS", _FakeDDGS)

    out = tools["web_search"](query="hello", max_results=2)
    assert out["ok"] is True
    assert len(out["results"]) == 2
    assert len(out["results"][0]["snippet"]) == 280
    assert out["results"][1]["snippet"] == "short"


def test_web_fetch_rejects_file_url(tools):
    out = tools["web_fetch"](url="file:///etc/passwd")
    assert out["ok"] is False


def test_web_fetch_html_extracts_text(tools, monkeypatch):
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.headers = {"content-type": "text/html; charset=utf-8"}
    fake_resp.text = (
        "<html><head><title>T</title><script>var x=1;</script></head>"
        "<body><h1>Hello</h1><p>World <b>bold</b></p></body></html>"
    )
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **kw: fake_resp)

    out = tools["web_fetch"](url="http://example.com")
    assert out["ok"] is True
    assert out["status_code"] == 200
    assert "Hello" in out["text"]
    assert "World" in out["text"]
    assert "var x" not in out["text"]


# ---------------------------------------------------------------------------
# YouTube transcript
# ---------------------------------------------------------------------------


class _FakeSnip:
    def __init__(self, text):
        self.text = text


class _FakeYT:
    def __init__(self, snippets):
        self._snips = snippets

    def fetch(self, video_id):  # noqa: ARG002
        return list(self._snips)


def _patch_yt(monkeypatch, snippets):
    import youtube_transcript_api as yta

    fake_class = lambda: _FakeYT(snippets)
    monkeypatch.setattr(yta, "YouTubeTranscriptApi", fake_class)


def test_youtube_transcript_parses_url_and_id(tools, monkeypatch):
    snips = [_FakeSnip("hello"), _FakeSnip("world")]
    _patch_yt(monkeypatch, snips)
    a = tools["fetch_youtube_transcript"](url_or_id="dQw4w9WgXcQ")
    assert a["ok"] is True
    assert a["video_id"] == "dQw4w9WgXcQ"
    assert "hello" in a["transcript"]

    b = tools["fetch_youtube_transcript"](
        url_or_id="https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s"
    )
    assert b["ok"] is True
    assert b["video_id"] == "dQw4w9WgXcQ"


def test_youtube_transcript_caps_chars(tools, monkeypatch):
    snips = [_FakeSnip("a" * 1000) for _ in range(50)]
    _patch_yt(monkeypatch, snips)
    out = tools["fetch_youtube_transcript"](url_or_id="dQw4w9WgXcQ", max_chars=2000)
    assert out["ok"] is True
    assert len(out["transcript"]) == 2000
    assert out["truncated"] is True


# ---------------------------------------------------------------------------
# Filesystem reads
# ---------------------------------------------------------------------------


def test_read_file_rejects_path_traversal(tools):
    out = tools["read_file"](path="../../etc/passwd")
    assert out["ok"] is False


def test_read_file_rejects_outside_repo(tools):
    out = tools["read_file"](path="/etc/passwd")
    assert out["ok"] is False


def test_read_file_reads_repo_file(tools):
    out = tools["read_file"](path="requirements.txt")
    assert out["ok"] is True
    assert "ddgs" in out["content"]


def test_list_files_caps_results(tools, tmp_path, monkeypatch):
    # Use a sandbox repo root with 1000 files to confirm cap.
    monkeypatch.setenv("JUDAS_REPO_ROOT", str(tmp_path))
    sub = tmp_path / "sub"
    sub.mkdir()
    for i in range(1000):
        (sub / f"f{i}.txt").write_text("x")
    out = tools["list_files"](path="sub", glob="*.txt", max_results=200)
    assert out["ok"] is True
    assert len(out["files"]) == 200


# ---------------------------------------------------------------------------
# Sandbox: evaluate_custom_strategy
# ---------------------------------------------------------------------------


def _synth_bars(n=80):
    rng = np.random.default_rng(42)
    base = 100 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
        "open": base, "high": base + 0.5, "low": base - 0.5,
        "close": base, "volume": 1000,
    })
    return df


def test_evaluate_custom_strategy_returns_signal():
    code = """
def evaluate(bars, params):
    last = float(bars['close'].iloc[-1])
    return {
        'direction': 'long',
        'entry': last,
        'stop': last - 1.0,
        'target': last + 2.0,
    }
"""
    bars = _synth_bars()
    out = csr.evaluate_custom_strategy(code=code, bars=bars, params={})
    assert out is not None
    assert out["direction"] == "long"
    assert out["target"] > out["entry"] > out["stop"]


def test_evaluate_custom_strategy_blocks_os_import():
    code = "import os\ndef evaluate(bars, params):\n    return {'os': os.listdir('/')}\n"
    out = csr.evaluate_custom_strategy(code=code, bars=_synth_bars(), params={})
    # __import__ is not in restricted builtins -> ImportError/NameError -> None.
    assert out is None


def test_evaluate_custom_strategy_blocks_open():
    code = """
def evaluate(bars, params):
    f = open('/etc/passwd')
    return {'data': f.read()}
"""
    out = csr.evaluate_custom_strategy(code=code, bars=_synth_bars(), params={})
    assert out is None


def test_evaluate_custom_strategy_blocks_dunder_import():
    code = """
def evaluate(bars, params):
    os = __import__('os')
    return {'data': os.listdir('/')}
"""
    out = csr.evaluate_custom_strategy(code=code, bars=_synth_bars(), params={})
    assert out is None


def test_evaluate_custom_strategy_timeout():
    code = """
def evaluate(bars, params):
    while True:
        pass
"""
    out = csr.evaluate_custom_strategy(code=code, bars=_synth_bars(), params={}, timeout_s=2)
    assert out is None


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------


def test_run_custom_backtest_returns_metrics():
    # Trivial evaluator: every 10th bar emit a long with tight target hit by next bar.
    code = """
def evaluate(bars, params):
    if len(bars) % 10 != 0:
        return None
    last = float(bars['close'].iloc[-1])
    return {
        'direction': 'long',
        'entry': last,
        'stop': last - 100.0,
        'target': last - 0.0001,  # immediate trigger as target<=entry trip
    }
"""
    # use simple deterministic bars
    bars = _synth_bars(60)
    res = csr.run_custom_backtest_on_bars(code=code, bars=bars, timeout_s=10)
    assert "n_signals" in res
    assert "n_wins" in res
    assert "total_pnl" in res
    assert "pf" in res
    assert isinstance(res["max_drawdown"], float)


def test_custom_backtest_passes_params_and_reports_net_contract_dollars():
    code = """
def evaluate(bars, params):
    if not params.get('enabled') or len(bars) != 3:
        return None
    entry = float(bars['close'].iloc[-1])
    return {'direction': 'long', 'entry': entry,
            'stop': entry - 1.0, 'target': entry + 1.0}
"""
    bars = pd.DataFrame([
        {"ts": pd.Timestamp(f"2026-01-01T00:0{i}:00Z"), "open": 100.0,
         "high": 100.2 if i != 3 else 101.1, "low": 99.5,
         "close": 100.0, "volume": 1}
        for i in range(6)
    ])

    disabled = csr.run_custom_backtest_on_bars(
        code=code, bars=bars, params={"enabled": False}, symbol="MGC", qty=2,
    )
    assert disabled["n_trades"] == 0

    result = csr.run_custom_backtest_on_bars(
        code=code, bars=bars, params={"enabled": True}, symbol="MGC", qty=2,
    )
    assert result["n_trades"] == 1
    assert result["total_pnl"] == pytest.approx(15.4)
    assert result["dollars_per_point"] == pytest.approx(10.0)
    assert result["cost_model"] == "v1_realistic_micros"


# ---------------------------------------------------------------------------
# DB-side custom strategy tools
# ---------------------------------------------------------------------------


_GOOD_CODE = """
def evaluate(bars, params):
    last = float(bars['close'].iloc[-1])
    return {'direction': 'long', 'entry': last, 'stop': last - 1.0, 'target': last + 2.0}
"""


def test_propose_custom_strategy_validates_compilation(tools, db):
    out = tools["propose_custom_strategy"](
        name="bad_one",
        symbol="MGC",
        code="def evaluate(bars, params)\n  return None",  # syntax error
        rationale="bad",
        backtest_metrics={},
    )
    assert out["ok"] is False
    with sqlite3.connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM custom_strategies").fetchone()[0]
    assert n == 0


def test_propose_custom_strategy_inserts_row(tools, db):
    out = tools["propose_custom_strategy"](
        name="trend_v1", symbol="MGC", code=_GOOD_CODE,
        rationale="trend follower", backtest_metrics={"pf": 1.5},
    )
    assert out["ok"] is True
    cid = out["custom_strategy_id"]
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT name, symbol, active FROM custom_strategies WHERE id = ?",
            (cid,),
        ).fetchone()
    assert row[0] == "trend_v1"
    assert row[1] == "MGC"
    assert row[2] == 1


def test_retire_custom_strategy_marks_retired(tools, db):
    prop = tools["propose_custom_strategy"](
        name="trend_v2", symbol="MGC", code=_GOOD_CODE,
        rationale="r", backtest_metrics={},
    )
    cid = prop["custom_strategy_id"]
    out = tools["retire_custom_strategy"](id=cid, reason="poor live perf")
    assert out["ok"] is True
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT active, retired_at_utc FROM custom_strategies WHERE id = ?",
            (cid,),
        ).fetchone()
        n = conn.execute(
            "SELECT COUNT(*) FROM auto_demotions WHERE strategy_family = 'custom'"
        ).fetchone()[0]
    assert row[0] == 0
    assert row[1] is not None
    assert n == 1


# ---------------------------------------------------------------------------
# portfolio_runtime dispatch
# ---------------------------------------------------------------------------


def test_portfolio_runtime_dispatches_custom_engine(tools, db, monkeypatch):
    # propose a custom strategy
    prop = tools["propose_custom_strategy"](
        name="dispatch_v1", symbol="MGC", code=_GOOD_CODE,
        rationale="r", backtest_metrics={},
    )
    cid = prop["custom_strategy_id"]

    monkeypatch.setenv("JUDAS_DB_PATH", db)

    # seed an active_strategies row with engine='custom'.
    with get_conn(db) as conn:
        cur = conn.execute(
            """
            INSERT INTO active_strategies
                (symbol, strategy_family, version, params_json, metrics_json,
                 state, activated_at_utc, notes)
            VALUES ('MGC','custom',1,?,'{}', 'active', '2026-01-01T00:00:00Z','seed')
            """,
            (json.dumps({
                "execution_engine": "custom",
                "custom_strategy_id": cid,
                "strategy_name": "dispatch_v1",
                "qty": 1,
            }),),
        )
        active_id = int(cur.lastrowid)

    from src.portfolio_runtime import evaluate_active_strategy

    bars = _synth_bars(50)
    active_row = {
        "id": active_id,
        "symbol": "MGC",
        "strategy_family": "custom",
        "version": 1,
        "params": {
            "execution_engine": "custom",
            "custom_strategy_id": cid,
            "strategy_name": "dispatch_v1",
            "qty": 1,
        },
    }
    fires = evaluate_active_strategy(active_row, {"MGC": bars})
    assert len(fires) == 1
    f = fires[0]
    assert f.symbol == "MGC"
    assert f.direction == "long"
    assert f.features.get("custom_strategy_id") == cid


# Regression test for finding 75f2741b (2026-08-19):
# Custom strategies were running on code defaults because the active row's
# params_json was NEVER merged into the evaluate() call. This test pins
# the contract that operator-tuned params in active_strategies.params_json
# MUST reach the strategy code and MUST shadow any same-named keys in the
# CSID's backtest_metrics_json shadow.
def test_portfolio_runtime_passes_active_params_to_custom_strategy(tools, db, monkeypatch):
    param_echo_code = """
def evaluate(bars, params):
    last = float(bars['close'].iloc[-1])
    return {
        'direction': 'long',
        'entry': last,
        # Use the active row's tuned target_r (must be 3.0, NOT default 1.5)
        'stop': last - 1.0,
        'target': last + float(params.get('target_r', 1.5)),
    }
"""
    prop = tools["propose_custom_strategy"](
        name="param_echo_v1", symbol="MGC", code=param_echo_code,
        rationale="r", backtest_metrics={"target_r": 1.5, "shadow_key": 99},
    )
    cid = prop["custom_strategy_id"]

    monkeypatch.setenv("JUDAS_DB_PATH", db)

    with get_conn(db) as conn:
        cur = conn.execute(
            """
            INSERT INTO active_strategies
                (symbol, strategy_family, version, params_json, metrics_json,
                 state, activated_at_utc, notes)
            VALUES ('MGC','custom',1,?,'{}', 'active', '2026-01-01T00:00:00Z','seed')
            """,
            (json.dumps({
                "execution_engine": "custom",
                "custom_strategy_id": cid,
                "strategy_name": "param_echo_v1",
                "qty": 1,
                "target_r": 3.0,  # operator-tuned value MUST reach evaluate()
            }),),
        )
        active_id = int(cur.lastrowid)

    from src.portfolio_runtime import evaluate_active_strategy

    bars = _synth_bars(50)
    active_row = {
        "id": active_id,
        "symbol": "MGC",
        "strategy_family": "custom",
        "version": 1,
        "params": {
            "execution_engine": "custom",
            "custom_strategy_id": cid,
            "strategy_name": "param_echo_v1",
            "qty": 1,
            "target_r": 3.0,
        },
    }
    fires = evaluate_active_strategy(active_row, {"MGC": bars})
    assert len(fires) == 1
    f = fires[0]
    # The active row's target_r=3.0 wins over the backtest_metrics shadow
    # target_r=1.5 (and over the code default 1.5). entry + 3.0 must equal
    # the target; entry + 1.5 would be a failure.
    assert abs((f.target - f.entry) - 3.0) < 1e-9, (
        f"active params not passed to evaluate(); got target-entry={f.target - f.entry}, expected 3.0"
    )
