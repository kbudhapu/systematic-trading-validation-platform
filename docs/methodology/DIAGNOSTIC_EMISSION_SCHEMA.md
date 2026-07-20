# Diagnostic Emission — SCHEMA v1 (dashboard contract)

**Status: v1 (2026-07-17).** Defined producer-side per PRE-RULING P1 — the display
adapts to producers, not vice versa. **Additive changes stay v1; a breaking change
requires v2.** This doc + the `DiagnosticReport` docstring
(`src/research/vtd/diagnostic_report.py`) are the source of truth. Dashboard review
requested; until then the version field lets a reader detect drift.

## Where it lives

- Producer type: `src.research.vtd.diagnostic_report.DiagnosticReport`
- Store: `vtd_diagnostic_reports` (append-only SQLite via the AsyncDBWriter;
  `src.persistence.diagnostic_report_store`). **Display-side readable /
  generation-side unreadable** — the 2.5 firewall: no generator/intake module imports
  the store.

## Every report carries (v1)

| field | JSON key | meaning |
|---|---|---|
| exp_id | `exp_id` | experiment id (e.g. EXP-NNN) |
| leg_id | `leg_id` | leg/instrument within the experiment |
| stage | `stage` | "1".."5" (VTD stage; validated) |
| verdict | `verdict` | terminal verdict — NEVER a blind/Phase-A value (see below) |
| diagnostics | `diagnostics_json` | the computed diagnostics: full-moment DSR (incl. band/bias provenance), PBO, haircut ×3, SPA, purged-WF per-window, cost-stress ladder |
| psd_flags | `psd_flags_json` | PSD gate flags |
| cost_observations | `cost_observations_json` | cost-stress observations |
| budget_key | `budget_key` | (instrument, period-window), timeframe-agnostic |
| budget_state | `budget_state` | trial-budget snapshot |
| **schema_version** | `schema_version` | **"v1"** — bump only on a breaking change |
| **seeds** | `seeds_json` | **every seed** that generated a stochastic figure (mcpt/bootstrap/pbo) — full replayability |
| **artifact_hashes** | `artifact_hashes_json` | content hashes of the source artifacts (returns, config) — byte-replay |
| created_utc | `created_utc` | store-side append timestamp |

`seeds` + `artifact_hashes` (v1 additions) make every report **replayable**: the exact
seed and the hash of the data it ran on are recorded, so a reader can reproduce any
figure and detect if the underlying artifact changed.

## Verdict-time-only emission (SFD 4.5)

A `DiagnosticReport` is **evidence** and is emitted ONLY at verdict time. `emit_report`
/ `emit_report_async` raise `PhaseAEmissionError` if the verdict is a non-terminal /
blind sentinel (`"" | PHASE_A | PHASE-A | BLIND | PENDING | NONE`, case-insensitive), so
a Phase-A/blind run can never leak a report into the append-only store.

## Evidence discipline (R1–R2)

The store is **append-only**; there is no update API. Re-emitting adds a row. Replication
artifacts (M3(b)/M4) are ADDED and LABELLED with an `evidence_class` of `'original'` vs
`'replication'` — originals are never removed or replaced. (The `evidence_class` column
lands with the M3/M4 emission work; v1 defines the report envelope + replayability
substrate those build on.)
