"""
Deterministic train / calibration / evaluation split (M5).

The split is a PURE FUNCTION of the pair's identity, not a materialized table:

    (key_a, key_b) -> stable order -> hash -> fraction in [0,1) -> partition

Two properties this buys us (and why it is a function, not a stored assignment):
  - Anyone can recompute the exact same split from the data alone — no split
    table to keep in sync, no risk of the stored assignment drifting from the
    algorithm.
  - A brand-new candidate pair falls into a deterministic partition the moment it
    exists, with no bookkeeping.

The split is what keeps scorer evaluation honest: parameters are learned on
`train`, probability calibration is fit on `calibration`, and `evaluation` is
touched ONCE per model version. Calibrating on the evaluation set — or, more
subtly, calibrating on the same rows used to report metrics — is leakage; the
third partition exists precisely to prevent it.

VERSIONING: the algorithm AND the ratios are the version. If either changes,
bump SPLIT_VERSION (e.g. 'v2') and keep the old branch, so a model evaluated
under 'v1' stays reproducible forever. Never mutate a released split in place.

NOTE: the split is a pure hash — deliberately NOT stratified by label. Stratifying
would require global knowledge of every pair's label, which would break both
properties above (recompute-from-data and auto-bucketing). The cost is that class
balance across partitions is left to the hash; the harness reports per-class,
per-partition counts so any starvation (e.g. too few `related` pairs to evaluate)
is visible rather than assumed away.
"""
from __future__ import annotations

import hashlib

from stevie_platform.canonical.candidates import order_pair

# The current split algorithm version. Bump on any change to the hashing or the
# ratios below; historical evaluations cite the version they ran under.
SPLIT_VERSION = "v1"

PARTITIONS = ("train", "calibration", "evaluation")

# Cumulative-fraction boundaries per version. Fractions are PART of the versioned
# algorithm — changing them is a new split version, not an edit to this one.
#   train        learn model parameters
#   calibration  fit probability calibration (Platt / isotonic)
#   evaluation   final, untouched benchmark
_RATIOS: dict[str, tuple[tuple[str, float], ...]] = {
    "v1": (("train", 0.60), ("calibration", 0.20), ("evaluation", 0.20)),
}


def _ratios(version: str) -> tuple[tuple[str, float], ...]:
    try:
        return _RATIOS[version]
    except KeyError:
        raise ValueError(
            f"unknown split version {version!r}; known: {sorted(_RATIOS)}") from None


def pair_fraction(key_a: str, key_b: str) -> float:
    """Map an unordered norm_key pair to a stable fraction in [0,1).

    Order-independent (via order_pair) and machine-independent (SHA-256, not
    Python's per-process-salted hash()). A NUL separator between the two keys
    prevents ('ab','c') and ('a','bc') from hashing as the same string."""
    lk, _, rk, _ = order_pair(key_a, 0, key_b, 0)
    digest = hashlib.sha256(f"{lk}\x00{rk}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def assign_split(key_a: str, key_b: str, *, version: str = SPLIT_VERSION) -> str:
    """Deterministic partition ('train' | 'calibration' | 'evaluation') for a
    norm_key pair under the given split version. Pure — same inputs always yield
    the same partition, on any machine, without any stored state."""
    frac = pair_fraction(key_a, key_b)
    acc = 0.0
    ratios = _ratios(version)
    for name, share in ratios:
        acc += share
        if frac < acc:
            return name
    return ratios[-1][0]  # guard against float rounding at the top of [0,1)
