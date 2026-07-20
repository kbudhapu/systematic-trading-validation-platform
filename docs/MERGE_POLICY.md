# MERGE POLICY

**Adopted 2026-07-12.** Supersedes the earlier "research self-merge / live-path operator-merge-only" rule. The gate was never *who clicks merge* — it is whether the **evidence exists**. Speed is granted where mistakes are cheap and reversible; approval is required where they are permanent.

## Claude Code MAY merge a PR when ALL are true

Any one false → **STOP, report, request approval.**

1. **TESTS PASS ON THE MERGED TREE** — not the branch. Locally if CI is unreliable. Quote the number ("N passed, 0 failed").
2. **NO CONFLICT**, or one resolved with the resolution **PROVEN AND SHOWN** (e.g. a superset argument with entry counts, and confirmation no other PR touches the file).
3. **NO TIER-1 SURFACE TOUCHED** (list below).
4. **NOTHING IRREVERSIBLE.** If it cannot be undone by `git revert` alone — a data migration, a schema change, a production-config edit — it is **TIER-1 BY DEFINITION.**
5. **THE PR IS WHAT IT SAYS IT IS.** Diff exceeds stated scope → STOP. Scope creep is how an unreviewed change ships inside a reviewed one.

## Merge trains — a train that does LESS than it said is a FAILED merge (amended 2026-07-13, X4)

§5 catches a merge that does **more** than it said (scope creep). It had no rule for a merge that does **less**. Add:

> **A merge train is complete only when EVERY named PR is merged or EXPLICITLY DEFERRED WITH A REASON, reported per-PR. An omission is a silent scope reduction and is treated as a failed merge.**

**EVIDENCE (2026-07-13):** a 14-PR train executed 8 and dropped 6, silently — and nobody noticed for a day. The tail included **#190, which carried a HARD PREREQUISITE** (the W-A leg-1 guard re-point) **for the PSD S9 amendment (A1) that shipped the next day.** A1 happened to survive it (A1 used a dedicated full function, so the two Gaussian copies stayed in sync and the guard stayed green — but that was luck, not the train's doing). The fix: **report per-PR at train end** — `merged` / `deferred: <reason>` for each named PR — and treat any unaccounted-for PR as a failed train.

## TIER-1 — OPERATOR APPROVAL, NO EXCEPTIONS

- `src/execution/`, `src/broker/`, `src/router/` — the live order path
- the orchestrator run loop
- `EXPERIMENT_REGISTRY` / any pre-registration / any doctrine doc (SFD 4.3: pre-registration is a **constitutionally non-automatable human joint**)
- any DB migration against production
- credentials, `.env`, deploy config
- anything changing **what gets collected** (collection is irreversible — SFD 6.2)
- PSD/VTD gate logic — the verdict path
- **A GATE'S OWN EVIDENCE.** A "test-only" change that weakens, stubs, or bypasses a safety gate **is a change to the gate.**

### Worked example (2026-07-12)

"Fix A" stubbed `_session_calendar.is_within_rth` to always-`True` to make three fault-injection tests pass. It was fast, plausible, and test-only — and it would have merged **clean, green, and unremarked**, having silently removed the session gate from the live path's fault tests. A gate that can never say "closed" cannot fail, and a test that cannot fail proves nothing. The correction: freeze the *clock* to a known instant and let the *real* calendar evaluate it, plus adversarial tests that prove the gate **fires** outside RTH.

## DEPLOY IS ALWAYS THE OPERATOR

Merging is not deploying. Tier-1 requires operator **approval**, not necessarily operator **execution**: Claude Code may execute a deploy only with the operator's explicit, in-writing approval for that specific deploy, under the operator's fences (stop-and-ask on anything ambiguous; never patch forward on production). Absent that, nothing reaches the droplet without the operator's hand.

## Rollback: fix-forward vs roll back (amended 2026-07-12)

The earlier "ANY RED on the deploy gate → ROLL BACK" rule was wrong: it would restore a *worse* baseline to remedy a *fixable* new-code fault. Corrected rule:

> **ROLL BACK when the deployed state is WORSE THAN THE BASELINE:** (a) data at risk, (b) new code actively harmful, or (c) the fault cannot be understood.
> A red that is **UNDERSTOOD, STRICTLY LESS HARMFUL THAN THE BASELINE, and FIXABLE IN THE REPO is a FIX-FORWARD** (halt or hold as appropriate, fix in the repo, re-test, re-tag, re-deploy).
> Rollback restores safety; it is not a punishment for imperfection.

Worked example (2026-07-12): the SIP-flip deploy hit `push_alerting_disabled` (systemd had no `EnvironmentFile`, so `.env`/`NTFY_TOPIC` never reached `os.getenv`) plus a caught log-kwarg `TypeError`. Rollback would have restored `a30931b` — the code with **no `log_fill` fix and no RTH gate**, which *provably manufactures phantom fills* — to remedy a missing env var and a log-line typo. Both faults were understood, strictly less harmful than the baseline, and repo-fixable → **fix-forward**, not rollback.

## The asymmetry

A bad merge to `main` is `git revert` — cheap. A bad **deploy, migration, or corrupted pre-registration** is not. Speed where mistakes are cheap; approval where they are permanent.

**WHEN IN DOUBT: STOP AND ASK.** A merge deferred by one message costs nothing.
