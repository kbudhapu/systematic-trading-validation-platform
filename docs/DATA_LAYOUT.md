# Data directory layout (`data/`)

The `data/` directory holds market-data caches and research outputs. **Almost none
of it is synced to git** — it is either regenerable or precious-but-large. This doc
records what lives where, what is safe to delete, and what must be backed up.

## What is tracked in git (load-bearing — kept for a fresh clone)

| File | Role | Regenerable? |
|---|---|---|
| `data/validate_production_24m.json` | Research-validation index consumed by the **ConfigurationParityAuditor** and the config parity tests (per-leg params + constraints). | Yes, but it is the pinned promotion artifact — treat as source-controlled config. |
| `data/adaptive_tuner_report.json` | Adaptive-tuner report referenced by tuner code/tests. | Yes (rerun the tuner). |
| `data/backtest_results.png` | Queue-8 baseline backtest figure (force-added deliverable under the global `*.png` ignore). | Yes (rerun the baseline backtest). |

Everything else under `data/` is git-ignored (see `.gitignore`).

## What is git-ignored (NOT synced)

- `data/*.json`, `data/*.jsonl`, `data/*.log`, `data/*.txt` — sweep results,
  checkpoints, run logs, and text reports. **Regenerable** research outputs.
- `data/*_parts/`, `data/*_parts_100mb/` — the multi-part decoupled-micro-suite
  CSV caches (e.g. `qqq_decoupled_micro_suite_parts_100mb/`). **Regenerable**,
  large; never sync.
- `data/parquet/*.parquet`, `data/*.csv` — bar-data caches (already ignored).
- `data/*.db`, WAL/journal sidecars — SQLite execution/research vaults (already
  ignored).
- `data/backups/` — local backup staging.

## Precious (back up out-of-band; do NOT rely on git)

- **The SIP decade bar cache** (the extended 2016→ history under `data/parquet/`
  and any `data/*.csv` decade extracts). This is expensive to re-pull from the
  data vendor and underpins every decade backtest. It is git-ignored by design —
  **keep an out-of-band backup** (external drive / object storage). Suggested
  path convention for the backup: `<backup_root>/mbappe/sip_decade_cache/`.
- The live **`research_vault.db` / `trading.db`** SQLite vaults (append-only
  ledgers: trial ledger, diagnostic reports, hash chain, alerts). Git-ignored;
  they are the execution source of truth and should be backed up with the host.

## Rule of thumb

If a file under `data/` is not in the "tracked in git" table above, assume it is
**regenerable** and safe to delete to reclaim space — except the SIP decade cache
and the SQLite vaults, which are precious and must be backed up out-of-band.

## Backup & disaster recovery (P2)

`scripts/backup_vault.py` snapshots the precious data into a dated archive under a
`BACKUP_ROOT` **outside the repo** (`$BACKUP_ROOT` in `.env`, or `~/mbappe_backups`
by default — point it at external/cloud storage):

- **SQLite vaults** (`research_vault.db`, `trading.db`) via the `sqlite3` `.backup`
  API — a consistent online snapshot, **WAL-safe, no writer pause**.
- **SIP decade parquet cache** (files + manifest).
- **Git-tracked docs** (`EXPERIMENT_REGISTRY.md`, `OVERNIGHT_QUEUE_LOG.md`,
  `docs/`) for a self-contained restore.

Retention keeps the last `keep` archives (default 8), pruning older ones.

```bash
python scripts/backup_vault.py                          # run a backup
python scripts/backup_vault.py --restore ARCHIVE --into DIR   # restore + verify
```

- **A backup is fiction until restored.** `verify_restore` (and
  `tests/test_backup_vault.py`) restores an archive and asserts each DB's row
  counts match the snapshot manifest + a sample parquet reads. Real-run demo:
  `research_vault.db` (30 tables) + `trading.db` (15 tables / 1799 rows) + 14
  parquet + docs → restore verify matched **True** for both.
- **Cadence:** **daily** while the soak / Phase-0 harness run; **weekly** otherwise.
- **Automation:** the maintenance daemon has an off-hours `vault_backup` job
  (03:00 ET) that no-ops unless `backup.enabled: true` in `config/env.yaml`
  (OFF by default). Enable it for the soak; otherwise run the script from cron.
- **Restore drill:** run the `--restore` command monthly and eyeball the matched
  report — the drill is the only thing that makes the backup real.

## Supabase — telemetry mirror / dashboard / config-command sync (E8)

Project `<redacted-supabase-project>` (`mbappe`, region `ACTIVE_HEALTHY`). This is the
**remote** half of the data layer; the local SQLite vaults above remain the source
of truth.

**Role & direction of flow:**

| Data | Direction | Written/read by | Supabase tables |
|---|---|---|---|
| orders, fills, rejections | local → remote (mirror write) | `SupabaseSync.sync_order_*` (live loop) | `orders` |
| bot runs, equity + leg-equity snapshots, risk state | local → remote (mirror write) | `SupabaseSync.sync_bot_run` / `sync_equity_snapshot` / `sync_leg_equity_snapshot` / `sync_risk_state` | `bot_runs`, `equity_snapshots`, … |
| system events, heartbeats | local → remote (mirror write) | `SupabaseSync.log_system_event` / `sync_heartbeat_ping` | system-event / heartbeat tables |
| strategy config, control commands | remote → local (sync read) | `config_listener`, `command_queue`, `config_watcher` | `strategies`, `control_commands` |
| dashboard | remote → operator | external dashboard reads the mirror | (all of the above) |

**Machine:** the single live trading box is the only writer of the mirror tables;
the dashboard and any second machine are read-only consumers (config/command rows
are the one remote→local path, used for cross-machine control, not for execution
decisions).

**Invariants (enforced):**

1. **SQLite is the local source of truth and primary read path.** Execution
   decisions read local state (in-memory + the SQLite-backed `_pending_order_store`
   and AsyncDBWriter WAL vault), never Supabase. Supabase is a best-effort WAL
   read-replica, matching the `db_queue` truth-model.
2. **No Supabase call is load-bearing in the live execution loop.** Every
   `SupabaseSync` write swallows a missing/raising client and returns gracefully;
   a Supabase outage leaves local outputs byte-identical. Enforced by
   `tests/test_supabase_best_effort.py` (client absent + client raising → the
   order/fill/heartbeat/event path never raises; the mirror order-id degrades to
   `None`, which the fill path tolerates).

**Open finding (E8):** the mirror writes on the order path
(`sync_order_submitted` → `sync_order_filled`) are still **synchronous**, so they
add network latency to the loop (correctness is unaffected per invariant 2). A
future latency fix should offload them, but it is **not output-trivial**:
`sync_order_submitted` returns the remote order-id that `sync_order_filled`
consumes for fill correlation, so a naive async offload changes the mirror's
correlation semantics. Logged, not fixed, in this behavior-preserving pass.
