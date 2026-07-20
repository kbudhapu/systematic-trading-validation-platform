"""
Configuration loading from YAML files and environment variables.

Separates static strategy parameters (config/) from secrets (.env).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def load_env() -> None:
    """Load the repo-root .env into os.environ so EVERY os.getenv() read (NTFY_TOPIC, SMTP/EMAIL,
    Supabase, webhooks, governance, SOAK_PROFILE, ...) is populated. R3 fix: previously load_dotenv
    lived only inside load_config(), which the SIP collector never calls -- so on the droplet (no
    systemd EnvironmentFile) .env was never in os.environ and every os.getenv read was silently
    empty; only dotenv_values(...) paths (ALPACA creds) worked. Call this FIRST at every entrypoint,
    from an EXPLICIT path (never cwd-walking). override=False so a real process env / a test
    monkeypatch always wins. Idempotent. Belt-and-braces with systemd EnvironmentFile=/opt/mbappe/.env."""
    load_dotenv(ROOT / ".env", override=False)


CONFIG_DIR = ROOT / "config"
# The single data root. Every store path (trading.db, research_vault.db, the
# circuit-breaker state file, backups, parquet, ...) derives from DATA_DIR, and
# DATA_DIR honours the MBAPPE_DATA_DIR env var. This is the one seam the test
# suite redirects (conftest sets MBAPPE_DATA_DIR to a per-session tmp dir BEFORE
# importing any src module) so no test can ever write production state. In
# production the env var is unset -> DATA_DIR == ROOT/"data" (unchanged).
DATA_DIR = Path(os.environ.get("MBAPPE_DATA_DIR") or (ROOT / "data"))
PARQUET_DIR = DATA_DIR / "parquet"
DB_PATH = DATA_DIR / "trading.db"


@dataclass
class RiskConfig:
    """Portfolio-level risk bounds enforced by the order router."""

    max_risk_per_trade_pct: float = 0.01
    max_drawdown_pct: float = 0.10
    max_position_pct: float = 0.95
    atr_period: int = 14
    long_only: bool = True


@dataclass
class BacktestConfig:
    """Pass/fail gates and simulation assumptions for backtests."""

    months: int = 6
    slippage_pct: float = 0.0005
    min_sharpe: float = 0.0
    max_drawdown_pct: float = 0.15


@dataclass
class CapacityGovernorConfig:
    """Retail equity-scaled turnover and participation limits."""

    equity_turnover_multiplier: float = 3.0
    max_participation_rate: float = 0.01
    min_participation_rate: float = 0.0025


@dataclass
class StrategyConfig:
    """Per-strategy deployment parameters from config/strategies/*.yaml."""

    strategy_id: str
    module: str
    symbol: str
    timeframe: str
    poll_interval_seconds: int
    params: dict
    enabled: bool = True
    environment: str = "paper"
    asset_class: str = "stock"
    # T2: RTH gate opt-in. Default DENY -- an equity leg is evaluated/submitted ONLY inside
    # RTH (parity with its RTH-filtered backtest). Set true ONLY as a registered, explicit
    # per-strategy decision to trade extended hours (with the correct TIF). Crypto is 24/7
    # regardless of this flag (asset_class == 'crypto').
    extended_hours: bool = False


@dataclass
class SoakConfig:
    """Soak-profile block from env.yaml `soak`. commissioning_legs (K4) is the
    allowlist of legs force-loaded in COMMISSIONING mode during a paper soak."""

    enabled: bool = False
    data_quality_enabled: bool = True
    heartbeat_stale_seconds: float = 120.0
    reconcile_on_boot: bool = True
    drawdown_breaker_test_pct: float = 0.05
    notifier: str = "log"
    commissioning_legs: list[str] = field(default_factory=list)


def assert_commissioning_gate_valid(environment: str, commissioning_legs: list[str]) -> None:
    """K4 cross-field rule (schema-level rejection): a non-empty commissioning
    allowlist is ONLY valid in a paper environment. A live environment with any
    commissioning leg is a config-load FAILURE (not a runtime refusal), and this
    must hold against remotely-synced config too -- call on every resolved config."""
    if environment == "live" and commissioning_legs:
        raise ValueError(
            "soak.commissioning_legs must be empty when environment=live "
            f"(commissioning mode is paper-only); got {list(commissioning_legs)}"
        )


@dataclass
class AppConfig:
    """Fully resolved runtime configuration."""

    environment: str
    data_ingestor: str
    broker: str
    risk: RiskConfig
    backtest: BacktestConfig
    capacity_governor: CapacityGovernorConfig
    soak: SoakConfig
    strategy: StrategyConfig
    strategies: list[StrategyConfig]
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_from: str
    email_to: str


def _load_yaml(path: Path) -> dict:
    """Read and parse a YAML config file."""
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _strategy_from_yaml(strat_data: dict, env_data: dict) -> StrategyConfig:
    params = dict(strat_data.get("params", {}) or {})
    if "cold_start_policy" not in params:
        from src.config.research_validation import load_research_validation_index

        symbol = str(strat_data.get("symbol", "")).upper()
        record = load_research_validation_index().get(symbol)
        if record is not None and "cold_start_policy" in record.constraints:
            params["cold_start_policy"] = record.constraints["cold_start_policy"]
    from src.ingestor.assets import infer_asset_class
    symbol = strat_data["symbol"]
    return StrategyConfig(
        strategy_id=strat_data["strategy_id"],
        module=strat_data["module"],
        symbol=symbol,
        timeframe=strat_data["timeframe"],
        poll_interval_seconds=strat_data.get("poll_interval_seconds", 900),
        params=params,
        enabled=strat_data.get("enabled", True),
        environment=strat_data.get("environment", env_data["environment"]),
        # K5: infer stock/crypto (as ConfigWatcher does) so the LOCAL charter config
        # used by commissioning force-load routes BTC/USD to the crypto data endpoint
        # -- else it 400s on the stocks/IEX bars endpoint -> bar_freshness degrade.
        asset_class=strat_data.get("asset_class") or infer_asset_class(symbol),
        # T2: default DENY -- extended-hours trading is an explicit registered opt-in.
        extended_hours=bool(strat_data.get("extended_hours", False)),
    )


def load_config(
    env_path: Path | None = None,
    strategy_path: Path | None = None,
    strategies_dir: Path | None = None,
) -> AppConfig:
    """
    Load application config from env.yaml, strategy yaml(s), and .env secrets.

    Loads all ``config/strategies/*.yaml`` when no single strategy_path is given.
  """
    load_dotenv(ROOT / ".env")

    env_data = _load_yaml(env_path or CONFIG_DIR / "env.yaml")
    risk = env_data.get("risk", {})
    bt = env_data.get("backtest", {})
    cg = env_data.get("capacity_governor", {})
    sk = env_data.get("soak", {}) or {}
    soak = SoakConfig(
        enabled=bool(sk.get("enabled", False)),
        data_quality_enabled=bool(sk.get("data_quality_enabled", True)),
        heartbeat_stale_seconds=float(sk.get("heartbeat_stale_seconds", 120.0)),
        reconcile_on_boot=bool(sk.get("reconcile_on_boot", True)),
        drawdown_breaker_test_pct=float(sk.get("drawdown_breaker_test_pct", 0.05)),
        notifier=str(sk.get("notifier", "log")),
        commissioning_legs=list(sk.get("commissioning_legs", []) or []),
    )
    # K4 cross-field: commissioning legs are paper-only -> fail config load under live.
    assert_commissioning_gate_valid(env_data["environment"], soak.commissioning_legs)

    if strategy_path is not None:
        yaml_paths = [strategy_path]
    elif strategies_dir is not None:
        yaml_paths = sorted(Path(strategies_dir).glob("*.yaml"))
    else:
        yaml_paths = sorted((CONFIG_DIR / "strategies").glob("*.yaml"))

    strategies = [_strategy_from_yaml(_load_yaml(p), env_data) for p in yaml_paths]
    enabled = [s for s in strategies if s.enabled]
    primary = enabled[0] if enabled else (strategies[0] if strategies else None)
    if primary is None:
        raise ValueError("no strategy yaml files found in config/strategies/")

    _base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    # P3 security: fail fast on a paper/live endpoint cross (paper keys against a
    # live URL or vice-versa) since one key pair + base-url toggle is used.
    from src.config.env_guard import assert_endpoint_matches_environment
    assert_endpoint_matches_environment(env_data["environment"], _base_url)

    return AppConfig(
        environment=env_data["environment"],
        data_ingestor=env_data["data_ingestor"],
        broker=env_data["broker"],
        risk=RiskConfig(
            max_risk_per_trade_pct=risk.get("max_risk_per_trade_pct", 0.01),
            max_drawdown_pct=risk.get("max_drawdown_pct", 0.10),
            max_position_pct=risk.get("max_position_pct", 0.95),
            atr_period=risk.get("atr_period", 14),
            long_only=risk.get("long_only", True),
        ),
        backtest=BacktestConfig(
            months=bt.get("months", 6),
            slippage_pct=bt.get("slippage_pct", 0.0005),
            min_sharpe=bt.get("min_sharpe", 0.0),
            max_drawdown_pct=bt.get("max_drawdown_pct", 0.15),
        ),
        capacity_governor=CapacityGovernorConfig(
            equity_turnover_multiplier=float(cg.get("equity_turnover_multiplier", 3.0)),
            max_participation_rate=float(cg.get("max_participation_rate", 0.01)),
            min_participation_rate=float(cg.get("min_participation_rate", 0.0025)),
        ),
        soak=soak,
        strategy=primary,
        strategies=strategies,
        alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        alpaca_base_url=_base_url,
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        email_from=os.getenv("EMAIL_FROM", ""),
        email_to=os.getenv("EMAIL_TO", ""),
    )
