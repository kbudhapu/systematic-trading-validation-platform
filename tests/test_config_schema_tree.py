"""E6/R6: the jsonschema-backed engine + the consolidated single-tree loader.

Locks: (1) the real env.yaml validates cleanly against the ONE composed schema
tree; (2) a malformed KNOWN block inside the tree is still rejected by its own
per-block schema; (3) unknown top-level blocks (strategy/risk/etc.) are permitted
so the whole config validates; (4) the tree registry covers every committed block
schema file (a new *.schema.json can't be silently left un-wired).
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from src.config.schema_check import (
    CONFIG_BLOCK_SCHEMAS, CONFIG_DIR, SchemaValidationError, load_config_schema_tree,
    validate_instance, validate_or_raise,
)

ENV_PATH = CONFIG_DIR / "env.yaml"


def _env() -> dict:
    with ENV_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_real_env_validates_against_consolidated_tree() -> None:
    assert validate_instance(_env(), load_config_schema_tree()) == []


def test_all_known_blocks_present_and_validated() -> None:
    env = _env()
    present = [b for b in CONFIG_BLOCK_SCHEMAS if b in env]
    # Every committed block schema corresponds to a real block in env.yaml.
    assert set(present) == set(CONFIG_BLOCK_SCHEMAS), present


def test_malformed_known_block_in_tree_rejected() -> None:
    env = copy.deepcopy(_env())
    env["validation"]["mcpt"]["n_perm"] = 500   # below doctrine floor 1000
    tree = load_config_schema_tree()
    with pytest.raises(SchemaValidationError):
        validate_or_raise(env, tree)

    env2 = copy.deepcopy(_env())
    env2["backup"]["bogus"] = 1                 # additionalProperties: false in block
    with pytest.raises(SchemaValidationError):
        validate_or_raise(env2, tree)


def test_unknown_top_level_blocks_permitted() -> None:
    env = copy.deepcopy(_env())
    env["some_future_block"] = {"anything": True}
    assert validate_instance(env, load_config_schema_tree()) == []


def test_tree_registry_covers_all_committed_block_schemas() -> None:
    on_disk = {p.name for p in Path(CONFIG_DIR).glob("*.schema.json")}
    wired = set(CONFIG_BLOCK_SCHEMAS.values())
    assert on_disk == wired, f"un-wired schema files: {on_disk ^ wired}"
