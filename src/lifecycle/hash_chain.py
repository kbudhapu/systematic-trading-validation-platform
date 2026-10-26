"""Config-hash chain: refit lineage + out-of-band tamper detection (LLD section 3).

The pinned hash covers a leg's STRUCTURE + its L4 adaptation policy. Parameter
values produced BY the registered refit procedure are legal and append a new LEAF
hash to the chain `(leg_id, epoch, parent_hash, leaf_hash, refit_evidence_ref)`,
whose parent_hash is the prior leaf. A leaf is valid iff the refit runner wrote it
-- so any parameter change arriving OUTSIDE the refit runner (manual edit, hotfix)
leaves no chain entry and is detected as out-of-band -> SAFE_MODE.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from src.engine.engine_preemption import RiskEscalationEngine, RiskEscalationLevel
from src.lifecycle.config_hash import canonical_config_hash
from src.persistence.hash_chain_store import (
    persist_chain_entries, read_chain, read_latest_leaf,
)

GENESIS_PARENT = None   # first leaf of a leg has no parent


def append_chain_entry(
    leg_id: str,
    leaf_hash: str,
    *,
    epoch: int,
    parent_hash: str | None,
    refit_evidence_ref: str | None,
    db_path: str | Path,
) -> None:
    persist_chain_entries([{
        "leg_id": leg_id, "epoch": epoch, "parent_hash": parent_hash,
        "leaf_hash": leaf_hash, "refit_evidence_ref": refit_evidence_ref,
    }], db_path)


def verify_chain(leg_id: str, db_path: str | Path) -> bool:
    """A chain is valid iff each entry's parent_hash equals the previous entry's
    leaf_hash (unbroken lineage), the first entry is genesis (no parent), and
    epochs are strictly increasing."""
    chain = read_chain(leg_id, db_path)
    if not chain:
        return True   # nothing recorded yet
    if chain[0]["parent_hash"] is not None:
        return False
    prev_leaf = chain[0]["leaf_hash"]
    prev_epoch = chain[0]["epoch"]
    for entry in chain[1:]:
        if entry["parent_hash"] != prev_leaf:
            return False
        if entry["epoch"] <= prev_epoch:
            return False
        prev_leaf = entry["leaf_hash"]
        prev_epoch = entry["epoch"]
    return True


def detect_out_of_band_change(
    leg_id: str,
    running_config: dict,
    db_path: str | Path,
    *,
    escalation: RiskEscalationEngine | None = None,
    report_sink: Callable[[dict], None] | None = None,
) -> bool:
    """Return True if the running config's hash is NOT the chain's latest leaf --
    i.e. a parameter change that no refit-runner entry accounts for. Trips SAFE_MODE
    and emits a DiagnosticReport (never adopts)."""
    running_hash = canonical_config_hash(running_config)
    latest = read_latest_leaf(leg_id, db_path)
    if latest is not None and running_hash == latest:
        return False
    # out-of-band: the running config is not the head of the recorded lineage
    if escalation is not None:
        escalation.transition(RiskEscalationLevel.ENTRY_GATE_HALT,
                              strategy_id=leg_id, commanded_by="config_chain_out_of_band")
    if report_sink is not None:
        report_sink({
            "kind": "config_chain_out_of_band", "leg_id": leg_id,
            "running_hash": running_hash, "chain_head": latest,
            "action": "SAFE_MODE", "adopted": False,
        })
    return True


class RefitRunner:
    """Writes the leg's hash-chain entries itself -- the ONLY legal producer of new
    leaf hashes (a leaf is valid iff it came through here)."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = db_path

    def promote_genesis(self, leg_id: str, config: dict, *, epoch: int = 0,
                        evidence_ref: str | None = None) -> str:
        """Record the first (genesis) leaf at promotion."""
        leaf = canonical_config_hash(config)
        append_chain_entry(leg_id, leaf, epoch=epoch, parent_hash=GENESIS_PARENT,
                           refit_evidence_ref=evidence_ref, db_path=self._db_path)
        return leaf

    def record_refit(self, leg_id: str, new_config: dict, *, epoch: int,
                     evidence_ref: str) -> str:
        """Record a legal refit: a new leaf whose parent is the current chain head."""
        parent = read_latest_leaf(leg_id, self._db_path)
        leaf = canonical_config_hash(new_config)
        append_chain_entry(leg_id, leaf, epoch=epoch, parent_hash=parent,
                           refit_evidence_ref=evidence_ref, db_path=self._db_path)
        return leaf
