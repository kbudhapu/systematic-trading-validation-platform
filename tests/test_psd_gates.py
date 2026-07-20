"""PSD gate calibration tests -- synthetic surfaces with ground truth by construction."""
from __future__ import annotations

import numpy as np

from src.research.psd import psd_gates as g

AX = [np.linspace(20.0, 60.0, 13), np.linspace(1.5, 4.0, 13)]   # 13 nodes/axis (>=5)


def _grid(fn):
    ii, jj = np.meshgrid(np.arange(13), np.arange(13), indexing="ij")
    return fn(ii, jj)


def _star(sm):
    return tuple(int(x) for x in np.unravel_index(int(sm.argmax()), sm.shape))


def test_a_plateau_passes_both_gates() -> None:
    # broad flat-topped hill (flat in a wide central region) + tiny noise
    rng = np.random.default_rng(0)
    raw = _grid(lambda i, j: np.minimum(1.0, 1.6 - 0.02 * ((i - 6) ** 2 + (j - 6) ** 2)))
    raw = np.clip(raw, 0.3, 1.0) + rng.normal(0, 0.003, raw.shape)
    sm = g.smooth_surface(raw)
    star = _star(sm)
    assert g.gate_p(sm, AX, star) is True, "broad plateau must PASS gate P"
    status, z = g.gate_s(sm, AX, star)
    assert status == "PASS", f"broad plateau must PASS gate S (got {status}, z={z:.2f})"


def test_b_needle_fails_at_least_one_gate() -> None:
    # flat noise floor with a single sharp spike -- must not survive BOTH gates
    rng = np.random.default_rng(1)
    raw = rng.uniform(0.05, 0.15, (13, 13))
    raw[6, 6] = 1.0
    sm = g.smooth_surface(raw)
    star = _star(sm)
    p = g.gate_p(sm, AX, star)
    status, z = g.gate_s(sm, AX, star)
    assert not (p and status == "PASS"), f"needle survived both gates (p={p}, s={status})"


def test_c_bimodal_returns_ensemble_multimodal() -> None:
    def two_hills(i, j):
        h1 = np.exp(-((i - 3) ** 2 + (j - 3) ** 2) / (2 * 1.2 ** 2))
        h2 = np.exp(-((i - 9) ** 2 + (j - 9) ** 2) / (2 * 1.2 ** 2))
        return np.maximum(h1, h2)
    sm = g.smooth_surface(_grid(two_hills))
    dec = g.select_point_or_ensemble(sm, AX, gate_s_status="PASS", k=10)
    assert dec["mode"] == "ensemble", f"bimodal must ensemble (got {dec})"
    assert "multimodal" in dec["reason"], dec["reason"]
    assert dec["n_components"] >= 2


def test_d_negative_plateau_fails_gate_p_p4() -> None:
    # broad hill whose peak is <= 0 -> doctrine P4 auto-fail
    raw = _grid(lambda i, j: -0.5 - 0.01 * ((i - 6) ** 2 + (j - 6) ** 2))
    sm = g.smooth_surface(raw)
    star = _star(sm)
    assert sm[star] <= 0.0
    assert g.gate_p(sm, AX, star) is False, "non-positive plateau center must auto-fail P4"


def test_e_gate_s_determinism() -> None:
    rng = np.random.default_rng(2)
    raw = _grid(lambda i, j: np.exp(-((i - 6) ** 2 + (j - 6) ** 2) / (2 * 3.0 ** 2))) + rng.normal(0, 0.02, (13, 13))
    sm = g.smooth_surface(raw)
    star = _star(sm)
    s1, z1 = g.gate_s(sm, AX, star, seed=42)
    s2, z2 = g.gate_s(sm, AX, star, seed=42)
    assert s1 == s2 and z1 == z2


def test_gate_s_coarse_axis_safeguard() -> None:
    # a 3-node axis (< min_nodes=5) must never silently PASS -> TOO_COARSE
    ax_coarse = [np.array([20.0, 40.0, 60.0]), np.linspace(1.5, 4.0, 13)]
    raw = np.random.default_rng(3).uniform(0, 1, (3, 13))
    sm = g.smooth_surface(raw)
    star = _star(sm)
    status, _ = g.gate_s(sm, ax_coarse, star)
    assert status == "TOO_COARSE", f"coarse axis must not silently pass (got {status})"


def test_ensemble_positions_equal_weight_average() -> None:
    import polars as pl
    a = pl.Series([1.0, -1.0, 0.0, 1.0])
    b = pl.Series([1.0, 1.0, 0.0, -1.0])
    out = g.ensemble_positions([a, b])
    assert out.to_list() == [1.0, 0.0, 0.0, 0.0]


def test_point_selection_when_unimodal_non_grey() -> None:
    raw = _grid(lambda i, j: np.minimum(1.0, 1.6 - 0.02 * ((i - 6) ** 2 + (j - 6) ** 2)))
    sm = g.smooth_surface(np.clip(raw, 0.3, 1.0))
    dec = g.select_point_or_ensemble(sm, AX, gate_s_status="PASS", k=10)
    assert dec["mode"] == "point" and dec["n_components"] == 1
