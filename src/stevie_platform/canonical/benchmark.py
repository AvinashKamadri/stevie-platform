"""
Frozen evaluation benchmark (M6, Slice 1) — pins the M5 evaluation set so that
every M6 result is measured against an IMMUTABLE target.

Why this exists
---------------
The train/calibration/evaluation split (`canonical/split.py`) is a pure hash of
the pair identity. That was the right choice for M5 (recompute-from-data, auto-
bucketing), but it has a sharp edge for M6's active-learning loop: when we add
newly labeled pairs to grow the training corpus, ~20% of them hash into the
`evaluation` bucket. That would silently *grow and change* the evaluation set —
so an M6 model would be measured on a different benchmark than M5, and any
"recall went up" claim would be comparing against a moving target.

The fix is to freeze the M5 evaluation set to disk, ONCE, and treat that file as
authoritative from then on:

  - The evaluation set is exactly the pairs in this manifest. It never grows when
    the corpus grows.
  - New active-learning labels are routed to train/calibration ONLY (they are a
    biased, model-selected sample — they must not enter a representative
    benchmark even if they hash there).
  - A regression guard (`assert_no_contamination`) makes leakage a hard, explicit
    failure instead of a convention someone can forget months from now.

Provenance / versioning
------------------------
This benchmark is frozen from (corpus `v2`, split `v1`) — the exact pair on which
the M5 v1.x models were evaluated. If either source version changes, that is a
NEW benchmark version: bump BENCHMARK_VERSION and freeze a new file; never mutate
a released one (same discipline as SPLIT_VERSION / MODEL_VERSION).
"""
from __future__ import annotations

import hashlib
import json

from stevie_platform.canonical.candidates import order_pair
from stevie_platform.canonical.recall import GOLD_DIR, load_corpus
from stevie_platform.canonical.split import assign_split

# The benchmark version and the (corpus, split) pair it is frozen from. Changing
# either source version is a new benchmark, not an edit to this one.
BENCHMARK_VERSION = "v1"
SOURCE_CORPUS = "v2"
SOURCE_SPLIT_VERSION = "v1"

PAIRS_PATH = GOLD_DIR / f"frozen_benchmark_{BENCHMARK_VERSION}.jsonl"
MANIFEST_PATH = GOLD_DIR / f"frozen_benchmark_{BENCHMARK_VERSION}.manifest.json"


class BenchmarkContaminationError(RuntimeError):
    """The regression guard fired: a training-pool pair collides with the frozen
    evaluation benchmark. Raising is the point — evaluation integrity is lost the
    moment a benchmark pair is trained on, so this must never pass silently."""


# --- pure core (no IO; unit-tested directly) --------------------------------

def ordered_pair(key_a: str, key_b: str) -> tuple[str, str]:
    """The (left_key, right_key) ordering used everywhere pairs are keyed."""
    lk, _, rk, _ = order_pair(key_a, 0, key_b, 0)
    return lk, rk


def build_frozen_pairs(gold: list[dict], *, split_version: str = SOURCE_SPLIT_VERSION) -> list[dict]:
    """Pure: the subset of `gold` whose pair hashes to the `evaluation`
    partition under `split_version`. This IS the M5 benchmark — the same set the
    v1.x models were scored on. Deterministically ordered by pair so the frozen
    file is byte-stable across regenerations."""
    out = []
    for g in gold:
        lk, rk = ordered_pair(g["key_a"], g["key_b"])
        if assign_split(lk, rk, version=split_version) == "evaluation":
            out.append({"left_key": lk, "right_key": rk, "label": g["label"]})
    out.sort(key=lambda r: (r["left_key"], r["right_key"]))
    return out


def _digest(pairs: list[dict]) -> str:
    """Order-independent content hash of the frozen pair set — lets `verify`
    detect any edit to the file (added/removed/relabeled pair) without trusting
    line order."""
    h = hashlib.sha256()
    for r in sorted(pairs, key=lambda r: (r["left_key"], r["right_key"])):
        h.update(f"{r['left_key']}\x00{r['right_key']}\x00{r['label']}\n".encode("utf-8"))
    return h.hexdigest()


def find_contamination(training_pairs, frozen: frozenset[tuple[str, str]]) -> list[tuple[str, str]]:
    """Pure set intersection: which training pairs collide with the benchmark.
    `training_pairs` must already be ordered (left<=right). Sorted for a stable,
    testable error message."""
    return sorted(set(training_pairs) & frozen)


# --- IO: freeze / load / verify / guard -------------------------------------

def freeze(*, force: bool = False) -> dict:
    """Materialize the frozen benchmark from (SOURCE_CORPUS, SOURCE_SPLIT_VERSION).

    Refuses to overwrite an existing benchmark unless `force=True` — the whole
    point is that it does not move. Run this ONCE (it is already committed); the
    guard against re-freezing protects that."""
    if PAIRS_PATH.exists() and not force:
        raise SystemExit(
            f"frozen benchmark already exists at {PAIRS_PATH} — it is immutable by "
            f"design. Re-freezing would move the target every result is compared "
            f"against. Pass force=True only to mint a NEW benchmark version.")

    gold, resolved, missing = load_corpus(SOURCE_CORPUS)
    if missing:
        raise SystemExit(
            f"cannot freeze benchmark: corpus {SOURCE_CORPUS!r} is missing "
            f"component(s) {missing} — the frozen set would be incomplete.")

    pairs = build_frozen_pairs(gold)
    from collections import Counter
    label_counts = dict(Counter(r["label"] for r in pairs))
    manifest = {
        "benchmark_version": BENCHMARK_VERSION,
        "source_corpus": resolved,
        "source_split_version": SOURCE_SPLIT_VERSION,
        "n_pairs": len(pairs),
        "label_counts": label_counts,
        "digest_sha256": _digest(pairs),
        "note": "Immutable M5 evaluation set. New labels go to train/calibration only.",
    }

    with PAIRS_PATH.open("w", encoding="utf-8") as f:
        for r in pairs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def load_frozen_pairs() -> list[dict]:
    if not PAIRS_PATH.exists():
        raise SystemExit(
            f"frozen benchmark not materialized: {PAIRS_PATH} — run `stevie benchmark --freeze`.")
    return [json.loads(line) for line in PAIRS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def frozen_pair_set() -> frozenset[tuple[str, str]]:
    """The immutable set of (left_key, right_key) evaluation pairs."""
    return frozenset((r["left_key"], r["right_key"]) for r in load_frozen_pairs())


def frozen_labels() -> dict[tuple[str, str], str]:
    return {(r["left_key"], r["right_key"]): r["label"] for r in load_frozen_pairs()}


def assert_no_contamination(training_pairs, *, frozen: frozenset[tuple[str, str]] | None = None) -> None:
    """REGRESSION GUARD (Slice 1 acceptance criterion).

    Raise BenchmarkContaminationError if ANY pair in the active-learning training
    pool appears in the frozen evaluation benchmark. `training_pairs` is an
    iterable of already-ordered (left_key, right_key) tuples. Call this before
    every fit on the expanded corpus."""
    frozen = frozen_pair_set() if frozen is None else frozen
    offenders = find_contamination(training_pairs, frozen)
    if offenders:
        raise BenchmarkContaminationError(
            f"{len(offenders)} training-pool pair(s) collide with the frozen "
            f"evaluation benchmark ({BENCHMARK_VERSION}) — evaluation integrity "
            f"would be destroyed. Route these to nowhere or exclude them; they "
            f"belong to the benchmark. First offenders: {offenders[:5]}")


def verify() -> dict:
    """Integrity check for the frozen file. Confirms:
      - the file's content digest matches the recorded manifest digest
        (detects any edit: added / removed / relabeled pair);
      - every frozen pair is still present in the current corpus with the SAME
        label (detects a benchmark pair silently changing meaning);
      - reports how many NEW eval-hashing pairs exist in the (possibly grown)
        corpus that are DELIBERATELY excluded from the frozen set — visibility,
        not a failure.
    Corpus growth alone never fails verification; only a change to a frozen pair
    does."""
    pairs = load_frozen_pairs()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else {}
    actual_digest = _digest(pairs)
    digest_ok = manifest.get("digest_sha256") == actual_digest

    gold, _resolved, _missing = load_corpus(SOURCE_CORPUS)
    corpus_labels = {ordered_pair(g["key_a"], g["key_b"]): g["label"] for g in gold}

    relabeled, dropped = [], []
    for r in pairs:
        pk = (r["left_key"], r["right_key"])
        cur = corpus_labels.get(pk)
        if cur is None:
            dropped.append(pk)
        elif cur != r["label"]:
            relabeled.append((pk, r["label"], cur))

    # New eval-hashing pairs in today's corpus that are NOT in the frozen set —
    # these are the "reserved" pairs held out of both training and the benchmark.
    frozen = frozenset((r["left_key"], r["right_key"]) for r in pairs)
    reserved = [pk for pk, _ in
                ((ordered_pair(g["key_a"], g["key_b"]), g) for g in gold)
                if assign_split(*pk, version=SOURCE_SPLIT_VERSION) == "evaluation" and pk not in frozen]

    return {
        "benchmark_version": BENCHMARK_VERSION,
        "n_pairs": len(pairs),
        "digest_ok": digest_ok,
        "recorded_digest": manifest.get("digest_sha256"),
        "actual_digest": actual_digest,
        "relabeled": relabeled,
        "dropped_from_corpus": dropped,
        "reserved_new_eval_pairs": len(reserved),
        "ok": digest_ok and not relabeled and not dropped,
    }


async def run_benchmark(*, do_freeze: bool = False, force: bool = False) -> dict:
    """CLI entry. Pure/offline — no DB. `--freeze` materializes (once); otherwise
    verify the existing frozen file."""
    if do_freeze:
        manifest = freeze(force=force)
        print("\n" + "=" * 60)
        print(f" FROZEN BENCHMARK {manifest['benchmark_version']}  -  materialized")
        print("=" * 60)
        print(f"  source corpus / split   {manifest['source_corpus']} / {manifest['source_split_version']}")
        print(f"  evaluation pairs        {manifest['n_pairs']}")
        print(f"  labels                  {manifest['label_counts']}")
        print(f"  digest                  {manifest['digest_sha256'][:16]}...")
        print("=" * 60 + "\n")
        return manifest

    v = verify()
    print("\n" + "=" * 60)
    print(f" FROZEN BENCHMARK {v['benchmark_version']}  -  verify")
    print("=" * 60)
    print(f"  evaluation pairs        {v['n_pairs']}")
    print(f"  content digest          {'OK' if v['digest_ok'] else 'MISMATCH'}")
    if v["relabeled"]:
        print(f"  [!] relabeled pairs     {len(v['relabeled'])}  (benchmark pair changed meaning!)")
    if v["dropped_from_corpus"]:
        print(f"  [!] dropped from corpus {len(v['dropped_from_corpus'])}")
    print(f"  reserved new-eval pairs {v['reserved_new_eval_pairs']}  (held out of training AND benchmark)")
    print(f"  status                  {'OK' if v['ok'] else 'FAILED'}")
    print("=" * 60 + "\n")
    return v
