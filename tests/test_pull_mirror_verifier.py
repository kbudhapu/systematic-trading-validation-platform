"""D2.3b: the production-mirror verifier keys hashes BY FILENAME and tolerates BOTH `sha256sum`
output forms -- GNU `<hash>  name` and git-bash's binary marker `<hash> *name`. A verifier whose
false-positive rate depends on which shell produced the output is not a verifier; this is exactly
how the D2.1 manual check nearly masked a real signal. No network, no droplet, no orders."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "pull_production_mirror",
    Path(__file__).resolve().parent.parent / "scripts" / "pull_production_mirror.py")
ppm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ppm)

_H = "0a8163afbb48ef38f793094c6cfe3f779889a779d147adda58f5e37c615cb142"


def test_parse_sha256_handles_both_output_formats():
    gnu = f"{_H}  paper_soak_x.jsonl\naa11  config/env.yaml\n"          # GNU: two spaces
    binm = f"{_H} *paper_soak_x.jsonl\naa11 *config/env.yaml\n"         # git-bash: '*' marker
    d_gnu = ppm._parse_sha256_lines(gnu)
    d_bin = ppm._parse_sha256_lines(binm)
    expected = {"paper_soak_x.jsonl": _H, "config/env.yaml": "aa11"}
    assert d_gnu == expected
    assert d_bin == expected
    # THE POINT: same files hashed by two different shells -> comparison SUCCEEDS.
    # A raw line-diff of the two would report ALL lines different (a false mismatch).
    assert d_gnu == d_bin


def test_parse_sha256_ignores_blanks_and_trailing_whitespace():
    assert ppm._parse_sha256_lines("\n   \n" + f"{_H}  f1\n") == {"f1": _H}


def test_live_window_is_conservative():
    # a soak log the running writer touched seconds ago must be skip-eligible, not verified.
    assert ppm.LIVE_WINDOW_S >= 60
