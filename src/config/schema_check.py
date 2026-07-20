"""Config schema validation (JSON-Schema draft-07).

E6/R6: the engine is now the pinned `jsonschema` library (Draft-07), replacing the
hand-rolled draft-07 subset validator. The PUBLIC API is unchanged --
`validate_instance` / `validate_or_raise` / `load_schema` / `SchemaValidationError`
behave identically for the config blocks, so every valid config still validates and
every malformed-block rejection test still rejects. Using the real library means
the full draft-07 keyword set is available (no more "supported subset" ceiling) and
the semantics are the standard ones rather than a bespoke reimplementation.

The per-block schemas under `config/*.schema.json` remain the source of truth for
each block; `load_config_schema_tree()` composes them into ONE root schema tree
behind a single loader so the whole env config can be validated in one call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"

# The additive config blocks, each with its own committed schema file. This is the
# single registry the consolidated tree is built from.
CONFIG_BLOCK_SCHEMAS: dict[str, str] = {
    "backup": "backup.schema.json",
    "data_quality": "data_quality.schema.json",
    "lifecycle": "lifecycle.schema.json",
    "param_selection": "param_selection.schema.json",
    "portfolio": "portfolio.schema.json",
    "regime_stress": "regime_stress.schema.json",
    "soak": "soak.schema.json",
    "validation": "validation.schema.json",
}


class SchemaValidationError(ValueError):
    """Raised when an instance does not conform to its schema."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) if self.errors else "schema validation failed")


def _format_error(err: Any) -> str:
    """Map a jsonschema ValidationError to a human-readable `<path>: <message>`
    string. The path is the instance location ("<root>" at the top); the message
    text carries the offending property/value (e.g. "'spa' is a required
    property"), preserving the error content callers assert on."""
    location = "/".join(str(p) for p in err.absolute_path)
    return f"{location or '<root>'}: {err.message}"


def validate_instance(instance: Any, schema: dict, path: str = "") -> list[str]:
    """Return a list of human-readable validation errors (empty == valid).

    `path`, when given, is prefixed to every reported location so nested callers
    can namespace their errors (config callers pass "")."""
    validator = Draft7Validator(schema)
    errors = [_format_error(e) for e in validator.iter_errors(instance)]
    if path:
        errors = [f"{path}.{e}" for e in errors]
    return errors


def validate_or_raise(instance: Any, schema: dict) -> None:
    """Validate ``instance`` against ``schema``; raise SchemaValidationError if invalid."""
    errors = validate_instance(instance, schema)
    if errors:
        raise SchemaValidationError(errors)


def load_schema(schema_path: str | Path) -> dict:
    with Path(schema_path).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_config_schema_tree(config_dir: str | Path = CONFIG_DIR) -> dict:
    """Compose the per-block schemas into ONE root draft-07 schema tree.

    Each known block is nested under `properties[<block>]` with its committed
    per-block schema (which keeps its own `additionalProperties: false`). The root
    permits unknown top-level keys (`additionalProperties: true`) so the real
    env.yaml -- which also carries strategy/risk/etc. blocks -- validates cleanly,
    while any malformed KNOWN block is still rejected by its own schema.
    """
    base = Path(config_dir)
    properties = {
        block: load_schema(base / filename)
        for block, filename in CONFIG_BLOCK_SCHEMAS.items()
    }
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }
