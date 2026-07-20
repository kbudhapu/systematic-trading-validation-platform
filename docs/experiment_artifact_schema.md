# Experiment Artifact Schema — v2

The **only** substrate the `/experiments` dashboard page may render. An artifact is the published
record of one experiment: its registry lifecycle (title/kind/status/verdict/criteria) plus the
family-normalized wave-1 statistics and PSD trial budget. Stored in Supabase `experiment_artifacts`
(migration 018), append-only, versioned. The dashboard renders **strictly** the keys below — no
client-side statistics, no numbers computed in TypeScript. Everything shown is a value published
here by `scripts/publish_experiment_artifact.py`, which reads `data/research_vault.db`.

> **v2 supersedes v1.** v1 assumed a hand-authored tearsheet JSON (`honesty.{dsr,pbo,mcpt,
> bootstrap_ci,haircut}`, `psd.plateau`, `wf.windows`) that **no producer in this repo emits**. The
> vault's actual shape is registry + per-family `wave1_results.statistics_json` + `psd_trial_ledger`
> (see `docs/experiments_data_inspection.md`). The publisher now BUILDS the artifact from the vault;
> statistics are extracted/normalized in Python, never in the browser.

## Assembly (publisher)
`experiment_registry` is an append-only lifecycle log (exp_id NOT unique). The publisher collapses to
the **latest row per exp_id** (`MAX(id)`), then folds `wave1_results` (join on `experiment_id =
exp_id`) and `psd_trial_ledger` (join on `queue_id = exp_id`). One artifact per distinct exp_id.

## Top-level keys
| key | type | meaning |
|---|---|---|
| `experiment_id` | string | stable id, e.g. `EXP-NNN` |
| `version` | integer | assigned by publisher; monotonic per experiment_id |
| `title` | string | registry title |
| `kind` | string | `EXPERIMENT` \| `QUARANTINED` \| `QUARANTINED_TOMBSTONE` \| `DOCTRINE` \| `STANDING` \| `PREAMBLE` |
| `status` | string | registry status (`ACTIVE`, `REJECTED`, `RESOLVED`, `REGISTERED`, `FROZEN`, …) |
| `verdict` | enum\|null | normalized `PASS` \| `PASS-FRAGILE` \| `REJECTED` \| `SHELVED`, or null |
| `verdict_detail` | string\|null | the raw registry ruling prose (the chip's hover) |
| `provenance` | string | always `REGISTRY` |
| `as_of` | string (ISO ts) | `decided_utc` ?? `registered_utc` ?? `created_utc` |
| `criteria_md` | string | the registry `content_md` — criteria/decision as markdown |
| `trials` | array | per-trial wave-1 statistics (below); `[]` if none |
| `psd` | object\|null | trial-budget / alpha-spend counts (below) |

`criteria_sha256 = sha256(criteria_md)` — binds the rendered verdict to the exact criteria text so a
verdict can never be silently re-scored under different criteria.

### Verdict normalization
Registry `verdict` is free-text prose (e.g. `"REJECTED-AT-TRIAGE (final)."`, `"PASS-FRAGILE — …"`,
even a bare date). Mapping: contains `PASS-FRAGILE`→`PASS-FRAGILE`, `REJECT`→`REJECTED`,
`SHELV`→`SHELVED`, else `PASS`→`PASS`. If the registry prose yields nothing, fall back to the best
wave-1 `triage_verdict`. The raw prose is preserved in `verdict_detail`.

## `trials[]` (family-normalized wave-1 statistics)
`wave1_results.statistics_json` is **per-family** — the cost-stress Sharpe / CI / MCPT-p live at
different keys per family (CAL/FLOW/TREND/L0-TECH/EVENT/XSEC). The publisher coalesces them into ONE
shape via a candidate-key search. Each trial:
| key | shape | notes |
|---|---|---|
| `trial_key`, `family`, `hypothesis`, `instrument` | string | trial identity |
| `n_events` | int | events in the trial |
| `triage_verdict` | string | per-trial triage ruling |
| `p_value` | number | primary one-sided MCPT p (top-level column) |
| `stats.sharpe_1x` | number\|null | 1× cost Sharpe/mean (`net_sharpe_1x`/`net_sharpe`/`ann_sharpe`/…); `sharpe_1x_key` records the source key |
| `stats.sharpe_2x`,`sharpe_4x` | number\|null | 2×/4× Sharpe ladder (CAL/FLOW/TREND) |
| `stats.stress_2x`,`stress_4x` | number\|null | 2×/4× **mean** cost-stress (XSEC/EVENT; distinct units) |
| `stats.ci` | `[lo,hi]`\|null | 95% CI; `ci_kind` = `sharpe`\|`mean` |
| `stats.mcpt_p` | number\|null | MCPT p; `mcpt_p_key` records which one-sided variant |
| `stats.dsr` | number\|null | Deflated Sharpe (only L0-TECH publishes it) |
| `stats.hit_rate` | number\|null | **payoff shape** — fraction of trades with positive net return (`hit_rate`/`win_rate`) |
| `stats.mean_win`,`mean_loss` | number\|null | **payoff shape** — mean net return of winning vs losing trades (`mean_win`/`avg_win`, `mean_loss`/`avg_loss`); `mean_loss` is signed (negative) |
| `stats.worst_trial_loss` | number\|null | **payoff shape** — the single most-negative trade net return, the left-tail (`worst_trial_loss`/`worst_trade`/`min_trade_return`) |

PBO is **absent** in the vault (removed from the schema). DSR is present only for L0-TECH trials.

### Payoff shape (`hit_rate`, `mean_win`, `mean_loss`, `worst_trial_loss`)
Sharpe, CI, and MCPT-p capture **mean**, **significance**, and **dispersion-of-the-mean** — they do
NOT distinguish a many-small-wins / rare-large-loss strategy from its mirror, and this program's
mechanisms skew toward the first (short-volatility-shaped). Without return shape, **no sizing or kill
rule can be derived from a registered result** (streak tolerance, per-trade risk, and the tail-tolerance
risk the program already identified are all invisible to the fields above). These four scalars close
that gap.

**Provenance & recomputability.** They are computed from the **per-trade net-return series**, which the
producers already build at run time (e.g. the `net` array in `exp0NN` stats) but currently **pop before
persistence** (it is transient, retained only long enough to feed the full-moment DSR). The publisher
reads these scalars from `statistics_json` via candidate-key search and emits `null` when absent.
- **New artifacts:** populate once a producer persists the four scalars from the per-trade series
  *before* the pre-persist pop (cheap — four numbers, not the series). That producer wiring is the
  POPULATION follow-up; this change is the schema/contract only.
- **Historical artifacts:** **cannot be recomputed** without re-running the experiment — the per-trade
  series was popped pre-persist and is gone. No backfill; they carry `null`.

## `psd` (trial budget / alpha-spend)
Folded from `psd_trial_ledger` rows for the exp_id: `{ grid_points, timeframes, objective_variants,
n_trials, entries }` (sums/maxes across ledger entries). Counts only — the vault records no metric
plateau, so there is no PSD stability curve.

## Rules
- The dashboard renders **only** these keys. Unknown keys are ignored.
- No number shown at `/experiments` is computed in the browser — all are published values.
- A correction re-publishes a **new version**; the table is append-only (018 triggers). The publisher
  is idempotent: an identical artifact (same `criteria_sha256` + `artifact_json`) is not re-inserted.
- Non-experiment kinds (DOCTRINE/STANDING/PREAMBLE) publish with `verdict=null`, `trials=[]`,
  `psd=null` — they are browsable registry records, not evaluated experiments.
