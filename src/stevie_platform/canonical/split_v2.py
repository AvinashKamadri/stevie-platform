"""
Three-way train/calibration/validation split (M6, Slice 2).

The v1 split (`canonical/split.py`) has four conceptual roles but three buckets:
train / calibration / evaluation. In v2 the **evaluation set is external** — it
is the frozen benchmark file (`canonical/benchmark.py`), pinned so it can never
drift. So `split_v2` only needs to partition the *non-benchmark* labeled pool,
and it introduces a **validation** bucket the v1 split lacked:

    train        fit model coefficients
    calibration  fit Platt scaling
    validation   model / threshold selection (so tuning never touches the benchmark)

Same pure-hash design as v1 (recompute-from-data, auto-bucketing, no stored
assignment) — it reuses `split.pair_fraction` verbatim, only the ratio table and
the bucket names differ. Benchmark pairs are removed by set membership *before*
this function is ever called; `split_v2` is never asked to bucket one.

VERSIONING: the algorithm AND ratios are the version. Bump SPLIT_V2_VERSION on
any change; a v2 model cites the split it trained under.
"""
from __future__ import annotations

import hashlib

from stevie_platform.canonical.candidates import order_pair

SPLIT_V2_VERSION = "v2"

PARTITIONS = ("train", "calibration", "validation")

# Cumulative-fraction boundaries per version. Part of the versioned algorithm —
# changing them is a new split version, not an edit to this one.
_RATIOS: dict[str, tuple[tuple[str, float], ...]] = {
    "v2": (("train", 0.70), ("calibration", 0.15), ("validation", 0.15)),
}


def _ratios(version: str) -> tuple[tuple[str, float], ...]:
    try:
        return _RATIOS[version]
    except KeyError:
        raise ValueError(
            f"unknown split_v2 version {version!r}; known: {sorted(_RATIOS)}") from None


def pair_fraction_v2(key_a: str, key_b: str, *, version: str = SPLIT_V2_VERSION) -> float:
    """Stable fraction in [0,1) for a pair, SALTED by the split version.

    Crucially INDEPENDENT of split.pair_fraction. The frozen benchmark is v1's
    top-20% fraction band, so the non-benchmark pool spans only the bottom 80%
    of v1's hash — reusing that hash would strand any v2 bucket above 0.8 (e.g.
    validation would always be empty). Salting with the version decorrelates the
    v2 partition from the benchmark removal, so train/calibration/validation
    distribute uniformly over the actual non-benchmark pool."""
    lk, _, rk, _ = order_pair(key_a, 0, key_b, 0)
    digest = hashlib.sha256(f"split_{version}\x00{lk}\x00{rk}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def assign_split_v2(key_a: str, key_b: str, *, version: str = SPLIT_V2_VERSION) -> str:
    """Deterministic partition ('train' | 'calibration' | 'validation') for a
    NON-benchmark norm_key pair. Pure — same inputs always yield the same
    partition, on any machine. There is deliberately no 'evaluation' bucket:
    evaluation lives in the frozen benchmark file, outside this hash."""
    frac = pair_fraction_v2(key_a, key_b, version=version)
    acc = 0.0
    ratios = _ratios(version)
    for name, share in ratios:
        acc += share
        if frac < acc:
            return name
    return ratios[-1][0]  # guard against float rounding at the top of [0,1)
