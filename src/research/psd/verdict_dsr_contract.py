"""A1 import-direction contract marker for the DSR verdict path (PSD S9).

`full_moment_live` (knife_edge_bands) requires, as condition (i) of retiring the interim
`DSR >= 0.95 + |dsr_bias|` posture, that the VERDICT PATH consumes
`cpcv_pbo.deflated_sharpe_ratio_full` EXCLUSIVELY -- i.e. the deprecated Gaussian
`cpcv_pbo.deflated_sharpe_ratio` is structurally unreachable from whatever module assembles
a live DSR verdict. This is a fail-closed API condition, not merely a schema/band_source check.

State today (2026-07-13): there is NO live verdict path -- the F1 window has zero validated legs
and zero resolved outcomes, so no module consumes a DSR at the gate yet. The flag is therefore
FALSE, and the interim posture stays in force (as ratified).

To flip it True LATER, when the verdict-assembly module is built:
  1. Have that module import ONLY `deflated_sharpe_ratio_full` (never the Gaussian scalar).
  2. Add a test asserting the Gaussian symbol is not reachable from the verdict path
     (e.g. it is not imported, and any attempt routes through the full estimator).
  3. Set VERDICT_PATH_FULL_MOMENT_EXCLUSIVE = True HERE, in the same change.
Never set it True by hand ahead of (1)+(2): `full_moment_live` is the machine-readable gate, and
a premature True would retire the safety margin while the Gaussian ceiling is still reachable.
"""
from __future__ import annotations

# FALSE until a full-moment-exclusive verdict path exists (see module docstring).
VERDICT_PATH_FULL_MOMENT_EXCLUSIVE: bool = False
