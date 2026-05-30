from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml
from dotenv import load_dotenv

load_dotenv()
_ROOT = Path(__file__).parent.parent


@dataclass
class IBKRConfig:
    host: str
    port: int
    data_client_id: int
    exec_client_id: int
    account: str


@dataclass
class NinjaTraderConfig:
    account: str
    host: str
    user: str
    python_exe: str
    outgoing_dir: str
    instrument_map: dict
    fill_timeout_s: float = 15.0
    fill_poll_s: float = 1.0


@dataclass
class SymbolConfig:
    symbol: str
    tick: float
    dollar_per_point: float
    timeframe: str


@dataclass
class JudasParams:
    confirmation_bars: int = 4
    pivot_length: int = 2
    target_r: float = 2.0
    stop_buffer_ticks: int = 2
    min_sweep_ticks: int = 3


@dataclass
class RiskConfig:
    max_contracts: int = 1
    max_open_positions: int = 12
    max_trades_per_day: int = 24
    atr_contraction_min_ratio: float = 0.60
    min_displacement_strength: float = 1.50
    min_displacement_body_ratio: float = 0.50
    max_sweep_age_bars: int = 2
    flatten_before_daily_close_minutes: int = 5


@dataclass
class CrewConfig:
    llm_model: str
    llm_base_url: str
    llm_provider: str = "openai"
    llm_reasoning_split: bool = True
    verbose: bool = True
    min_quality_score: int = 6


@dataclass
class Config:
    mode: str
    account: str
    ibkr: IBKRConfig
    symbols: List[SymbolConfig]
    judas: JudasParams
    risk: RiskConfig
    crew: CrewConfig
    route: str = "ibkr"
    ninjatrader: "NinjaTraderConfig | None" = None
    db_path: str = "judas_crew.db"
    minimax_api_key: str = field(default_factory=lambda: os.environ.get("MINIMAX_API_KEY", ""))


def load_config(path: str | None = None) -> Config:
    cfg_path = Path(path) if path else _ROOT / "config.yaml"
    with open(cfg_path) as f:
        raw = yaml.safe_load(f)

    # The IBKR account itself is a paper account — the mode field on
    # config.yaml is informational, not a safety lock. Don't refuse to
    # start over it.
    mode = raw.get("mode", "paper")

    ibkr_raw = raw["ibkr"]
    ibkr = IBKRConfig(
        host=os.environ.get("IBKR_HOST", ibkr_raw["host"]),
        port=int(os.environ.get("IBKR_PORT", ibkr_raw["port"])),
        data_client_id=int(os.environ.get("IBKR_DATA_CLIENT_ID", ibkr_raw["data_client_id"])),
        exec_client_id=int(os.environ.get("IBKR_EXEC_CLIENT_ID", ibkr_raw["exec_client_id"])),
        account=ibkr_raw["account"],
    )
    symbols = [SymbolConfig(**s) for s in raw.get("symbols", [])]
    strat = raw.get("strategy", {}).get("judas", {})
    judas = JudasParams(**strat) if strat else JudasParams()
    risk_raw = raw.get("risk", {})
    risk = RiskConfig(**risk_raw) if risk_raw else RiskConfig()
    crew_raw = raw.get("crew", {})
    crew = CrewConfig(
        llm_model=crew_raw.get("llm_model", "MiniMax-M2.7"),
        llm_base_url=crew_raw.get("llm_base_url", "https://api.minimax.io/v1"),
        llm_provider=crew_raw.get("llm_provider", "openai"),
        llm_reasoning_split=crew_raw.get("llm_reasoning_split", True),
        verbose=crew_raw.get("verbose", True),
        min_quality_score=crew_raw.get("min_quality_score", 6),
    )
    route = os.environ.get("JUDAS_ROUTE", raw.get("execution", {}).get("route", "ibkr"))
    nt_raw = raw.get("ninjatrader")
    ninjatrader = None
    if nt_raw:
        ninjatrader = NinjaTraderConfig(
            account=nt_raw["account"],
            host=nt_raw.get("host", "100.108.151.36"),
            user=nt_raw.get("user", "nqtrader"),
            python_exe=nt_raw["python_exe"],
            outgoing_dir=nt_raw["outgoing_dir"],
            instrument_map={str(k): str(v) for k, v in (nt_raw.get("instrument_map") or {}).items()},
            fill_timeout_s=float(nt_raw.get("fill_timeout_s", 15.0)),
            fill_poll_s=float(nt_raw.get("fill_poll_s", 1.0)),
        )
    return Config(
        mode=mode,
        account=raw.get("account", ibkr_raw.get("account", "DUH860616")),
        ibkr=ibkr,
        symbols=symbols,
        judas=judas,
        risk=risk,
        crew=crew,
        route=route,
        ninjatrader=ninjatrader,
        db_path=raw.get("db_path", "judas_crew.db"),
        minimax_api_key=os.environ.get("MINIMAX_API_KEY", ""),
    )
