"""VTD Task 1: the additive `validation` block in config/env.yaml must validate
against config/validation.schema.json, the parity auditor must stay green with
the block present, and a malformed validation block must be rejected."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from src.config import CONFIG_DIR, load_config
from src.config.parity_auditor import ConfigurationParityAuditor
from src.config.schema_check import (
    SchemaValidationError,
    load_schema,
    validate_instance,
    validate_or_raise,
)

_ENV_PATH = CONFIG_DIR / "env.yaml"
_SCHEMA_PATH = CONFIG_DIR / "validation.schema.json"


def _load_validation_block() -> dict:
    with _ENV_PATH.open(encoding="utf-8") as handle:
        env = yaml.safe_load(handle)
    assert "validation" in env, "additive validation block missing from env.yaml"
    return env["validation"]


def test_repo_validation_block_matches_schema() -> None:
    """The block committed to env.yaml validates cleanly against the schema."""
    block = _load_validation_block()
    schema = load_schema(_SCHEMA_PATH)
    errors = validate_instance(block, schema)
    assert errors == [], f"validation block failed schema: {errors}"


def test_validation_block_matches_doctrine_values() -> None:
    """Values are byte-for-byte the doctrine section 3 defaults (no drift)."""
    block = _load_validation_block()
    assert block["doctrine_version"] == "1.0"
    assert block["mcpt"] == {
        "n_perm": 1000, "block": "auto", "session_aware": True, "entry_p": 0.05,
    }
    assert block["haircut"]["methods"] == ["bonferroni", "holm", "bhy"]
    assert block["cost_stress"]["slippage_multipliers"] == [1.0, 2.0, 4.0]
    assert block["spa"]["universe"] == "full_ledger"
    assert block["spa"]["fwer"] == 0.05
    assert block["holdout"] == {"weeks": 26, "shots": 1}


def test_parity_auditor_green_with_validation_block_present() -> None:
    """Adding the validation block does not disturb the parity auditor: the repo
    config still passes (guardrails intact)."""
    config = load_config()
    auditor = ConfigurationParityAuditor()
    auditor.audit_app_config(config)  # raises on any drift


def test_param_selection_block_still_valid_alongside_validation() -> None:
    """The pre-existing param_selection block and its schema are untouched."""
    with _ENV_PATH.open(encoding="utf-8") as handle:
        env = yaml.safe_load(handle)
    assert "param_selection" in env, "param_selection block must remain (byte-identical)"
    ps_schema = load_schema(CONFIG_DIR / "param_selection.schema.json")
    assert validate_instance(env["param_selection"], ps_schema) == []


def test_malformed_validation_block_extra_key_rejected() -> None:
    block = copy.deepcopy(_load_validation_block())
    block["bogus_key"] = 1
    schema = load_schema(_SCHEMA_PATH)
    with pytest.raises(SchemaValidationError):
        validate_or_raise(block, schema)


def test_malformed_validation_block_wrong_type_rejected() -> None:
    block = copy.deepcopy(_load_validation_block())
    block["mcpt"]["entry_p"] = "not-a-number"
    schema = load_schema(_SCHEMA_PATH)
    with pytest.raises(SchemaValidationError):
        validate_or_raise(block, schema)


def test_malformed_validation_block_missing_required_rejected() -> None:
    block = copy.deepcopy(_load_validation_block())
    del block["spa"]
    schema = load_schema(_SCHEMA_PATH)
    errors = validate_instance(block, schema)
    assert any("spa" in e for e in errors)


def test_malformed_validation_block_bad_enum_rejected() -> None:
    block = copy.deepcopy(_load_validation_block())
    block["haircut"]["methods"] = ["bonferroni", "fdr_unknown"]
    schema = load_schema(_SCHEMA_PATH)
    with pytest.raises(SchemaValidationError):
        validate_or_raise(block, schema)


def test_malformed_validation_block_bad_n_perm_rejected() -> None:
    """n_perm below the doctrine floor of 1000 must fail (minimum bound)."""
    block = copy.deepcopy(_load_validation_block())
    block["mcpt"]["n_perm"] = 500
    schema = load_schema(_SCHEMA_PATH)
    with pytest.raises(SchemaValidationError):
        validate_or_raise(block, schema)


def test_schema_check_self_consistency_on_param_selection_malformed() -> None:
    """The generic validator also catches drift in the param_selection schema,
    proving it is reusable (not hardcoded to the validation block)."""
    schema = load_schema(CONFIG_DIR / "param_selection.schema.json")
    bad = {"doctrine_version": "9.9"}  # wrong const + missing required
    errors = validate_instance(bad, schema)
    assert errors, "generic validator should reject a bad param_selection block"


def test_validation_schema_file_is_wellformed_json() -> None:
    schema = load_schema(_SCHEMA_PATH)
    assert schema["$schema"].startswith("http://json-schema.org/draft-07")
    assert schema["additionalProperties"] is False
    assert "validation block" in schema["title"]
