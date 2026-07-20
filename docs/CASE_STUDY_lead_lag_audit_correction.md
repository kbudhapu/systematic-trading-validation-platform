# Case study — a correct verdict reached for a partly wrong reason

The other two case studies —
[`CASE_STUDY_mean_reversion_rejection.md`](CASE_STUDY_mean_reversion_rejection.md) and
[`CASE_STUDY_momentum_stage2_rejection.md`](CASE_STUDY_momentum_stage2_rejection.md) — show
hypotheses being rejected. This one shows what happened **after** a rejection: an audit found the
stated reasoning was wrong, and the record was corrected three days later even though the verdict
did not change.

## The hypothesis

Intra-industry lead-lag — the well-documented tendency for information to arrive first in large,
heavily-followed names and reach smaller peers with a delay. A published anomaly class, chosen
deliberately for that reason.

Registered **2026-07-23**, with pass criteria locked at the registration commit. To pass, a trial
had to satisfy **all** of:

- matched-adjusted net mean return > 0
- p < 0.025 (Bonferroni across a three-trial family)
- cluster-bootstrap 95% CI excluding zero
- full-moment Deflated Sharpe Ratio > 0.95
- CI still excluding zero under a 2× cost stress

## Scale

| window | events | triggers |
|---|---|---|
| DEEP | 101,885 | 9,553 |
| PRIMARY | 47,554 | 5,401 |

Data-integrity gates were pre-declared with their own ceilings and cleared: thin-data share **0%**,
event collapse **2.04%** against an **8%** ceiling, cooldown suppressions **3,154**. Significance
used **1,000** Monte Carlo permutation draws.

This mattered later. A design with this many events is not going to fail for lack of power, so a
null here means something a small-sample null does not.

## The verdict

**REJECTED — neither window passed.** The measured direction was opposite the hypothesis.

That triggered a second action. Under the project's stopping doctrine (SFD 2.2(b)), a result that is
statistically significant but points **against** the registered hypothesis is not tradeable — it is
**quarantined**. It becomes a caged finding: recorded, assigned an ID, and permanently barred from
being tested on the data that suggested it. The reasoning is that flipping the sign of a failed
hypothesis and trading the reverse is the purest form of fitting to noise, and it feels like
discovery while you're doing it. So the machinery removes the option rather than relying on
discipline.

## Three days later, the audit

An independent audit pass re-examined the verdict on **2026-07-26** and found the stated reasoning
did not hold.

The original record said both windows were significantly negative. The audit ran the
negative-direction placebo test — shuffling the trigger while holding everything else fixed — and
found the two windows behaved differently:

| window | placebo p | reading |
|---|---|---|
| DEEP | 0.003 | clears; effect is trigger-specific |
| PRIMARY | 0.196 | **fails**; observed mean sits inside its own placebo null |

PRIMARY's apparent significance had come from the cluster-bootstrap confidence interval alone — and
that interval was dominated by a deterministic cost drag applied identically to real and placebo
triggers. The interval excluded zero because *every* strategy on that construction would have. It
was measuring the cost model, not the signal.

**The disposition did not change: still REJECTED.** But the quarantine was re-grounded on the DEEP
window alone, and the registry was corrected in place.

## What this demonstrates

**A significance test can be right for the wrong reason, and a bootstrap CI is the usual culprit.**
A CI that excludes zero because a deterministic drag moves every draw in the same direction is not
evidence about the signal. The placebo test caught this; the original battery did not, because
nothing in it asked whether the CI's exclusion was trigger-specific.

**Verdicts are auditable after the fact, and the audit runs whether or not the verdict is in
doubt.** This one was reviewed despite already being a rejection — the cheapest thing in the world
would have been to leave a correct answer alone.

**Corrections are made in the record, not appended to it.** The original claim was wrong and is now
marked wrong at the place someone reading the registry will encounter it. An experiment log that
only accumulates is not a log, it is a scrapbook.

*No signal parameters, universe thresholds, cost assumptions, effect magnitudes, or per-era
breakdowns appear in this document. The sample counts, p-values, and gate structure describe the
validation machinery; the market-facing quantities are omitted by policy.*
