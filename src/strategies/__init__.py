"""Strategy layer — public showcase excerpt.

The full multi-strategy registry and the parameter-bound strategy instances are
intentionally omitted from this public repository: active and pass-fragile
strategies, and their tuned parameter values, are not published (see the repo
README — "live strategies omitted to protect active edge").

What remains is the reusable strategy chassis (`BaseStrategy`), the generic
mean-reversion logic, and the regime filter — the pieces needed to follow the
one worked example in ``docs/CASE_STUDY_mean_reversion_rejection.md``, in which
the validation pipeline statistically rejects a mean-reversion hypothesis.
"""

from src.strategies.base import BaseStrategy

__all__ = ["BaseStrategy"]
