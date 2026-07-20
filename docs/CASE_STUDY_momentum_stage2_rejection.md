# Case study — a hypothesis that cleared triage, then failed five of six gates

Companion to [`CASE_STUDY_mean_reversion_rejection.md`](CASE_STUDY_mean_reversion_rejection.md),
which shows a hypothesis dying at first contact. This one passed the first gate with a strong result
and was destroyed by the second. See also
[`CASE_STUDY_lead_lag_audit_correction.md`](CASE_STUDY_lead_lag_audit_correction.md), where a
rejection is audited and re-grounded three days later.

## The hypothesis

Cross-sectional momentum — long the top-decile performers, measured against the benchmark. Among the
most-replicated anomalies in the published literature. Nothing here is novel, which is the point: it
tests the machinery rather than the idea.

## The pre-registration came before the data

Stage-2 criteria were drafted and locked on **2026-07-10**. The dataset they would be tested against
had not been purchased — the pre-registration says so: *the underlying data does not yet exist on
project machines*.

Pre-registration only means something if it is genuinely blind, and "we wrote the criteria first" is
easy to claim. Here it is provable from commit history: the criteria predate the data's existence,
so they cannot have been shaped by it.

Six criteria, all required to pass:

| | criterion |
|---|---|
| **C1** | primary-window mean > 0 **and** bootstrap 95% CI excludes zero |
| **C2** | full-moment Deflated Sharpe Ratio > 0.95 at the family trial count |
| **C3** | survives a 2x cost stress |
| **C4** | mean still > 0 with a single exceptional calendar year removed |
| **C5** | replicates on deep history (2000-2016) at permutation p < 0.05 |
| **C6** | positive across both halves of a split universe |

C3 through C6 all ask the same question in different clothes: does this result depend on one
convenient thing — cheap execution, one good year, one era, one slice of the universe?

## Triage: a strong pass

Across **106 monthly formations**, a permuted-rank Monte Carlo test returned **p = 0.001** against a
Bonferroni-corrected **alpha = 0.0167**. The observed value sat **above the entire permutation null**
— not in its tail, past it.

The registry verdict was **PASS-FRAGILE**, not PASS. Clearing triage earns a hypothesis a harder
test, not a place in the portfolio.

## Stage 2: five of six failed

Run **2026-07-19** against criteria fixed nine days earlier, before the data existed.

| | result |
|---|---|
| **C1** | **FAIL** — mean positive, but the bootstrap CI included zero |
| **C2** | **FAIL** — DSR **0.3132** against a 0.95 gate; no guards fired |
| **C3** | **FAIL** — CI still spanned zero at 2x cost; negative at 4x |
| **C4** | **FAIL** — removing one calendar year turned the mean **negative** |
| **C5** | **FAIL** — see below |
| **C6** | PASS — positive in both universe halves |

**Verdict: REJECTED-AT-STAGE-2 — final for this rule on this data.**

### C5 is the one that matters

Deep-history replication over 2000-2016 did not merely fail to confirm. The effect was
**significantly negative**, with the observed value falling **below the entire permutation null** at
**p ~ 0.001**.

Sixteen years of out-of-sample history said the opposite of the years that generated the hypothesis.

That triggered a second action. Under the project's stopping doctrine (SFD 2.2(b)), a result that is
significant but points **against** the registered hypothesis is not tradeable — it is
**quarantined**: recorded, assigned an ID, and permanently barred from being tested on the data that
suggested it. Inverting a failed hypothesis and trading the reverse is the purest form of fitting to
noise, and it feels like discovery while you do it. The machinery removes the option rather than
relying on discipline.

## What the failures say together

The registry's own note is blunter than a summary would be: selection was working; the *net level*
was never distinguishable from zero. C1 and C3 both say the interval spans zero. C4 says the
positive mean was one year wearing a decade's clothing. C5 says the sign flips out of sample.

**DSR 0.31 against a 0.95 gate is the number to sit with.** A permutation p of 0.001 and a Deflated
Sharpe Ratio of 0.31 are not in conflict — they answer different questions. The permutation test asks
whether the ranking carried information. The DSR asks whether the Sharpe survives being deflated for
how many things were tried and for the shape of the return distribution. The first said yes. The
second said the result is what a search of this size produces by construction.

**A p-value of 0.001 is an input, not a result.** Five other conditions applied, and a genuinely
fragile effect fails several of them.

**The dataset is now burned for this hypothesis.** Having been used to reject it, it cannot later be
used to resurrect it — recorded in the registry's burn ledger so a future version of this idea cannot
quietly re-run against the same substrate and report the second-best answer.

**Rejection is the modal outcome and the system is built for it.** Nothing here was a process
failure. The pipeline did exactly what it exists to do, on a hypothesis given every chance.

*Effect magnitudes, confidence-interval bounds, cost assumptions, signal parameters, and universe
specifics are omitted. The counts, p-values, DSR, and gate structure describe the validation
machinery; the market-facing quantities are not published.*
