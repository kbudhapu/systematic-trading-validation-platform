"""Per-asset production gates for mean-reversion parameter sweeps."""

from __future__ import annotations

from dataclasses import dataclass

FULL_DD_MAX = 0.08

# Legacy global constants (research / exhaustive sweeps)
HOLDOUT_SHARPE_MIN = 2.0
FULL_RETURN_MIN = 0.15
HOLDOUT_DD_MAX = 0.10
FULL_TRADES_MIN = 30
HOLDOUT_TRADES_MIN = 10
PROFIT_FACTOR_MIN = 1.4
TRAIN_HOLDOUT_SHARPE_RATIO = 0.65


@dataclass(frozen=True)
class AssetProductionGate:
    holdout_sharpe_min: float
    full_return_min: float


PRODUCTION_GATES: dict[str, AssetProductionGate] = {
    "SPY": AssetProductionGate(holdout_sharpe_min=2.0, full_return_min=0.15),
    "QQQ": AssetProductionGate(holdout_sharpe_min=1.65, full_return_min=0.18),
}

# SPY conservative 24m production sieve
SPY_CONSERVATIVE_HOLDOUT_SHARPE_MIN = 2.0
SPY_CONSERVATIVE_FULL_RETURN_MIN = 0.10
SPY_CONSERVATIVE_FULL_DD_MAX = 0.08
SPY_CONSERVATIVE_FULL_TRADES_MIN = 80
SPY_CONSERVATIVE_HOLDOUT_TRADES_MIN = 25


def _symbol_key(symbol: str) -> str:
    return symbol.upper()


def production_gate(symbol: str) -> AssetProductionGate:
    key = _symbol_key(symbol)
    if key not in PRODUCTION_GATES:
        raise ValueError(f"no production gate for symbol {symbol}")
    return PRODUCTION_GATES[key]


def min_holdout_sharpe(symbol: str) -> float:
    return production_gate(symbol).holdout_sharpe_min


def min_full_return(symbol: str) -> float:
    return production_gate(symbol).full_return_min


def calmar(row: dict) -> float:
    return row["full_return"] / max(row["full_max_drawdown"], 1e-9)


def passes_production_sieve(row: dict, symbol: str | None = None) -> bool:
    sym = _symbol_key(symbol or str(row["symbol"]))
    gate = production_gate(sym)
    if row["holdout_sharpe"] < gate.holdout_sharpe_min:
        return False
    if row["full_return"] < gate.full_return_min:
        return False
    if row["full_max_drawdown"] > FULL_DD_MAX:
        return False
    return True


def production_sieve_summary(symbol: str) -> str:
    gate = production_gate(symbol)
    return (
        f"holdout_sharpe>={gate.holdout_sharpe_min}, "
        f"full_return>={gate.full_return_min:.0%}, full_dd<={FULL_DD_MAX:.0%}"
    )


def rank_sweep_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["holdout_sharpe"], calmar(r)), reverse=True)


def passes_spy_allweather_sieve(row: dict) -> bool:
    holdout_sh = row["holdout_sharpe"]
    train_sh = row["train_sharpe"]
    if holdout_sh < SPY_ALLWEATHER_HOLDOUT_SHARPE_MIN:
        return False
    if row["full_return"] < SPY_ALLWEATHER_FULL_RETURN_MIN:
        return False
    if row["full_max_drawdown"] > SPY_ALLWEATHER_FULL_DD_MAX:
        return False
    if train_sh > 0 and holdout_sh < SPY_ALLWEATHER_TRAIN_HOLDOUT_SHARPE_RATIO * train_sh:
        return False
    return True


def rank_spy_allweather_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda r: (r["holdout_sharpe"], -r["full_max_drawdown"]),
        reverse=True,
    )


# SPY all-weather (legacy 6% DD sieve)
SPY_ALLWEATHER_HOLDOUT_SHARPE_MIN = 2.0
SPY_ALLWEATHER_FULL_RETURN_MIN = 0.10
SPY_ALLWEATHER_FULL_DD_MAX = 0.06
SPY_ALLWEATHER_TRAIN_HOLDOUT_SHARPE_RATIO = 0.70


def passes_spy_conservative_24m_sieve(row: dict) -> bool:
    if row["holdout_sharpe"] < SPY_CONSERVATIVE_HOLDOUT_SHARPE_MIN:
        return False
    if row["full_return"] < SPY_CONSERVATIVE_FULL_RETURN_MIN:
        return False
    if row["full_max_drawdown"] > SPY_CONSERVATIVE_FULL_DD_MAX:
        return False
    if row["full_trades"] < SPY_CONSERVATIVE_FULL_TRADES_MIN:
        return False
    if row["holdout_trades"] < SPY_CONSERVATIVE_HOLDOUT_TRADES_MIN:
        return False
    return True


def spy_conservative_24m_sieve_summary() -> str:
    return (
        f"holdout_sharpe>={SPY_CONSERVATIVE_HOLDOUT_SHARPE_MIN}, "
        f"full_return>={SPY_CONSERVATIVE_FULL_RETURN_MIN:.0%}, "
        f"full_dd<={SPY_CONSERVATIVE_FULL_DD_MAX:.0%}, "
        f"full_trades>={SPY_CONSERVATIVE_FULL_TRADES_MIN}, "
        f"holdout_trades>={SPY_CONSERVATIVE_HOLDOUT_TRADES_MIN}"
    )


def conservative_24m_fail_reasons(row: dict) -> list[str]:
    reasons: list[str] = []
    if row["holdout_sharpe"] < SPY_CONSERVATIVE_HOLDOUT_SHARPE_MIN:
        reasons.append(
            f"holdout_sharpe={row['holdout_sharpe']:.2f}<{SPY_CONSERVATIVE_HOLDOUT_SHARPE_MIN}"
        )
    if row["full_return"] < SPY_CONSERVATIVE_FULL_RETURN_MIN:
        reasons.append(
            f"full_return={row['full_return']:.1%}<{SPY_CONSERVATIVE_FULL_RETURN_MIN:.0%}"
        )
    if row["full_max_drawdown"] > SPY_CONSERVATIVE_FULL_DD_MAX:
        reasons.append(
            f"full_dd={row['full_max_drawdown']:.1%}>{SPY_CONSERVATIVE_FULL_DD_MAX:.0%}"
        )
    if row["full_trades"] < SPY_CONSERVATIVE_FULL_TRADES_MIN:
        reasons.append(
            f"full_trades={row['full_trades']}<{SPY_CONSERVATIVE_FULL_TRADES_MIN}"
        )
    if row["holdout_trades"] < SPY_CONSERVATIVE_HOLDOUT_TRADES_MIN:
        reasons.append(
            f"holdout_trades={row['holdout_trades']}<{SPY_CONSERVATIVE_HOLDOUT_TRADES_MIN}"
        )
    return reasons


def rank_spy_conservative_24m_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda r: (r["holdout_sharpe"], -r["full_max_drawdown"]),
        reverse=True,
    )


def rank_spy_near_misses(rows: list[dict]) -> list[dict]:
    def score(row: dict) -> tuple:
        passed = 5 - len(conservative_24m_fail_reasons(row))
        return (passed, row["holdout_sharpe"], -row["full_max_drawdown"])

    return sorted(rows, key=score, reverse=True)


def spy_allweather_sieve_summary() -> str:
    return (
        f"holdout_sharpe>={SPY_ALLWEATHER_HOLDOUT_SHARPE_MIN}, "
        f"full_return>={SPY_ALLWEATHER_FULL_RETURN_MIN:.0%}, "
        f"full_dd<={SPY_ALLWEATHER_FULL_DD_MAX:.0%}, "
        f"holdout_sharpe>={SPY_ALLWEATHER_TRAIN_HOLDOUT_SHARPE_RATIO:.0%}*train_sharpe"
    )


def passes_quality_sieve(row: dict) -> bool:
    """Strict research sieve (legacy exhaustive / analysis scripts)."""
    holdout_sh = row["holdout_sharpe"]
    train_sh = row["train_sharpe"]
    if holdout_sh < HOLDOUT_SHARPE_MIN:
        return False
    if row["full_return"] < FULL_RETURN_MIN:
        return False
    if row["full_max_drawdown"] > FULL_DD_MAX:
        return False
    if row["holdout_max_drawdown"] > HOLDOUT_DD_MAX:
        return False
    if row["full_trades"] < FULL_TRADES_MIN:
        return False
    if row["holdout_trades"] < HOLDOUT_TRADES_MIN:
        return False
    if row["full_profit_factor"] < PROFIT_FACTOR_MIN:
        return False
    if train_sh > 0 and holdout_sh < TRAIN_HOLDOUT_SHARPE_RATIO * train_sh:
        return False
    return True


def sieve_summary() -> str:
    return (
        f"holdout_sharpe>={HOLDOUT_SHARPE_MIN}, full_return>={FULL_RETURN_MIN:.0%}, "
        f"full_dd<={FULL_DD_MAX:.0%}, holdout_dd<={HOLDOUT_DD_MAX:.0%}, "
        f"full_trades>={FULL_TRADES_MIN}, holdout_trades>={HOLDOUT_TRADES_MIN}, "
        f"profit_factor>={PROFIT_FACTOR_MIN}, "
        f"holdout_sharpe>={TRAIN_HOLDOUT_SHARPE_RATIO:.0%}*train_sharpe"
    )
