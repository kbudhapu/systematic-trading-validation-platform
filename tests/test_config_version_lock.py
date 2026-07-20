"""Tests for optimistic Supabase configuration version locking."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.control.config_watcher import ConfigWatcher
from src.engine.config_engine import ConfigurationPrecedenceResolver


def test_verify_version_lock_allows_matching_versions() -> None:
    watcher = ConfigWatcher()
    watcher._strategy_versions["mean_reversion_qqq"] = 3
    resolver = ConfigurationPrecedenceResolver()
    with patch.object(
        resolver,
        "_fetch_remote_strategy_row",
        return_value={"version_id": 3, "updated_at": "2026-06-24T12:00:00+00:00"},
    ):
        result = resolver.verify_version_lock(
            "mean_reversion_qqq",
            config_watcher=watcher,
        )
    assert result.allowed is True
    assert result.reason == "version_match"


def test_verify_version_lock_resolves_conflict() -> None:
    watcher = ConfigWatcher()
    watcher._strategy_versions["mean_reversion_qqq"] = 2
    resolver = ConfigurationPrecedenceResolver()
    with patch.object(
        resolver,
        "_fetch_remote_strategy_row",
        return_value={"version_id": 5, "updated_at": "2026-06-24T12:00:00+00:00"},
    ):
        with patch("src.engine.config_engine.persistence.log_system_event") as log_event:
            result = resolver.verify_version_lock(
                "mean_reversion_qqq",
                config_watcher=watcher,
            )
    assert result.allowed is False
    assert result.conflict_resolved is True
    assert result.reason == "version_mismatch"
    log_event.assert_called_once()
    assert log_event.call_args.args[0] == "CONFIG_VERSION_CONFLICT_RESOLVED"


def test_write_remote_strategy_update_applies_when_versions_match() -> None:
    watcher = ConfigWatcher()
    watcher._strategy_versions["mean_reversion_qqq"] = 4
    watcher._strategy_uuids["mean_reversion_qqq"] = "uuid-qqq"
    resolver = ConfigurationPrecedenceResolver()

    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.update.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = MagicMock(
        data=[{"version_id": 5, "updated_at": "2026-06-24T13:00:00+00:00"}]
    )

    with patch("src.engine.config_engine.get_supabase", return_value=mock_client):
        with patch.object(
            resolver,
            "_fetch_remote_strategy_row",
            return_value={"version_id": 4, "updated_at": "2026-06-24T12:00:00+00:00"},
        ):
            result = resolver.write_remote_strategy_update(
                "mean_reversion_qqq",
                {"params": {"entry_z": 1.5}},
                config_watcher=watcher,
            )
    assert result.applied is True
    assert result.version_id == 5
    update_payload = mock_table.update.call_args.args[0]
    assert "version_id" not in update_payload
    assert "updated_at" not in update_payload


def test_enforce_preflight_config_alignment_when_tokens_match() -> None:
    watcher = ConfigWatcher()
    watcher._strategy_versions["mean_reversion_qqq"] = 3
    watcher._strategy_updated_at["mean_reversion_qqq"] = "2026-06-24T12:00:00+00:00"
    resolver = ConfigurationPrecedenceResolver()
    with patch.object(
        resolver,
        "_fetch_remote_strategy_row",
        return_value={"version_id": 3, "updated_at": "2026-06-24T12:00:00+00:00"},
    ):
        result = resolver.enforce_preflight_config_alignment(
            watcher,
            strategy_ids=("mean_reversion_qqq",),
        )
    assert result.aligned is True
    assert result.reason == "aligned"


def test_enforce_preflight_config_alignment_reloads_on_mismatch() -> None:
    watcher = ConfigWatcher()
    watcher._strategy_versions["mean_reversion_qqq"] = 2
    watcher._strategy_updated_at["mean_reversion_qqq"] = "2026-06-24T12:00:00+00:00"
    resolver = ConfigurationPrecedenceResolver()
    remote_row = {
        "version_id": 5,
        "updated_at": "2026-06-24T13:00:00+00:00",
    }

    def get_latest_side_effect():
        watcher._strategy_versions["mean_reversion_qqq"] = remote_row["version_id"]
        watcher._strategy_updated_at["mean_reversion_qqq"] = remote_row["updated_at"]
        from src.config import load_config

        return load_config()

    with patch.object(
        resolver,
        "_fetch_remote_strategy_row",
        return_value=remote_row,
    ):
        with patch.object(watcher, "get_latest", side_effect=get_latest_side_effect):
            result = resolver.enforce_preflight_config_alignment(
                watcher,
                strategy_ids=("mean_reversion_qqq",),
            )
    assert result.aligned is True
    assert result.reason == "realigned_after_reload"


def test_verify_version_lock_detects_updated_at_mismatch() -> None:
    watcher = ConfigWatcher()
    watcher._strategy_versions["mean_reversion_qqq"] = 3
    watcher._strategy_updated_at["mean_reversion_qqq"] = "2026-06-24T12:00:00+00:00"
    resolver = ConfigurationPrecedenceResolver()
    with patch.object(
        resolver,
        "_fetch_remote_strategy_row",
        return_value={"version_id": 3, "updated_at": "2026-06-24T13:00:00+00:00"},
    ):
        result = resolver.verify_version_lock(
            "mean_reversion_qqq",
            config_watcher=watcher,
        )
    assert result.allowed is False
    assert result.reason == "updated_at_mismatch"
