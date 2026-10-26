"""G2.1: the additive lifecycle (LLD) and portfolio (PAD) blocks in
config/env.yaml must validate against their schemas, match the doctrine section-7
values, keep the parity auditor green, and reject malformed blocks."""
from __future__ import annotations

import copy

import pytest
import yaml

from src.config import CONFIG_DIR, load_config
from src.config.parity_auditor import ConfigurationParityAuditor
from src.config.schema_check import (
    SchemaValidationError, load_schema, validate_instance, validate_or_raise,
)

_ENV = CONFIG_DIR / "env.yaml"


def _block(name: str) -> dict:
    with _ENV.open(encoding="utf-8") as fh:
        env = yaml.safe_load(fh)
    assert name in env, f"additive {name} block missing from env.yaml"
    return env[name]


# ---- lifecycle (LLD) ------------------------------------------------------- #

def test_lifecycle_block_matches_schema() -> None:
    assert validate_instance(_block("lifecycle"), load_schema(CONFIG_DIR / "lifecycle.schema.json")) == []


def test_lifecycle_values_match_doctrine() -> None:
    b = _block("lifecycle")
    assert b["doctrine_version"] == "1.0"
    assert b["states"] == ["CANDIDATE", "VALIDATED", "PAPER", "ACTIVE", "WATCH", "SAFE_MODE", "RETIRED"]
    assert b["watch"] == {"sizing_factor": 0.5, "max_weeks": 8,
                          "min_armed_weeks": 12, "min_armed_trades": 40}
    assert b["monitors"]["cusum"] == {"k": 0.5, "h": 5.0}
    assert b["monitors"]["drawdown_bound"]["multiplier"] == 1.25
    assert b["monitors"]["safe_mode_episodes_to_retire"] == 2
    assert b["hash_chain"] == {"algo": "sha256", "enforce_on": ["startup", "reload", "refit"]}


@pytest.mark.parametrize("mutate", [
    lambda b: b.update({"bogus": 1}),
    lambda b: b["monitors"]["cusum"].update({"h": "five"}),
    lambda b: b.__setitem__("doctrine_version", "9.9"),
    lambda b: b["watch"].update({"sizing_factor": 2.0}),
    lambda b: b.pop("hash_chain"),
])
def test_malformed_lifecycle_block_rejected(mutate) -> None:
    b = copy.deepcopy(_block("lifecycle"))
    mutate(b)
    with pytest.raises(SchemaValidationError):
        validate_or_raise(b, load_schema(CONFIG_DIR / "lifecycle.schema.json"))


# ---- portfolio (PAD) ------------------------------------------------------- #

def test_portfolio_block_matches_schema() -> None:
    assert validate_instance(_block("portfolio"), load_schema(CONFIG_DIR / "portfolio.schema.json")) == []


def test_portfolio_values_match_doctrine() -> None:
    b = _block("portfolio")
    assert b["doctrine_version"] == "1.0"
    assert b["vol_target_annual"] == 0.12
    assert b["admission"] == {"max_abs_corr": 0.35, "min_overlap_weeks": 52}
    assert b["caps"] == {"max_cluster": 0.40, "max_leg": 0.25, "shortvol_cluster": 0.15}
    assert b["convergence_watch"] == {"corr_threshold": 0.6, "consecutive_weeks": 4}
    assert b["budgeting"]["max_step"] == 0.20


@pytest.mark.parametrize("mutate", [
    lambda b: b.update({"bogus": 1}),
    lambda b: b["admission"].update({"max_abs_corr": 1.5}),
    lambda b: b["budgeting"].update({"rebalance": "weekly"}),
    lambda b: b.__setitem__("doctrine_version", "2.0"),
    lambda b: b.pop("caps"),
])
def test_malformed_portfolio_block_rejected(mutate) -> None:
    b = copy.deepcopy(_block("portfolio"))
    mutate(b)
    with pytest.raises(SchemaValidationError):
        validate_or_raise(b, load_schema(CONFIG_DIR / "portfolio.schema.json"))


# ---- parity auditor stays green; guardrails intact ------------------------- #

def test_parity_auditor_green_with_governance_blocks() -> None:
    ConfigurationParityAuditor().audit_app_config(load_config())


def test_prior_additive_blocks_still_present() -> None:
    """The lifecycle/portfolio additions do not disturb the earlier additive
    blocks or the per-strategy guardrails."""
    with _ENV.open(encoding="utf-8") as fh:
        env = yaml.safe_load(fh)
    for name in ("param_selection", "validation", "data_quality", "lifecycle", "portfolio"):
        assert name in env
