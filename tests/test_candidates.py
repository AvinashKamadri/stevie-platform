"""Unit tests for the DB-free core of merge-candidate generation + the recall
harness. The blockers themselves hit Postgres and are exercised by `cli recall`;
here we pin the pure logic that decides identity, dedup, and recall accounting."""
from stevie_platform.canonical.candidates import (
    Pair, acronym_pairs, content_tokens, merge_pairs, order_pair,
)
from stevie_platform.canonical.recall import evaluate, wilson_interval


# --- order_pair: unordered identity by key ---------------------------------
def test_order_pair_is_direction_independent():
    assert order_pair("b", 2, "a", 1) == order_pair("a", 1, "b", 2) == ("a", 1, "b", 2)


# --- merge_pairs: union + dedup + reason merge -----------------------------
def test_merge_pairs_dedupes_and_unions_reasons():
    raw = [
        ("acme", 1, "acme corp", 2, "trigram"),
        ("acme corp", 2, "acme", 1, "rare_token"),  # same pair, other direction
    ]
    [pair] = merge_pairs(raw)
    assert (pair.left_key, pair.right_key) == ("acme", "acme corp")
    assert pair.reasons == ("rare_token", "trigram")  # sorted, both kept


def test_merge_pairs_drops_self_pairs():
    assert merge_pairs([("acme", 1, "acme", 1, "trigram")]) == []


def test_merge_pairs_keeps_distinct_pairs_separate():
    pairs = merge_pairs([
        ("a", 1, "b", 2, "trigram"),
        ("a", 1, "c", 3, "trigram"),
    ])
    assert {(p.left_key, p.right_key) for p in pairs} == {("a", "b"), ("a", "c")}


# --- content_tokens: rare-token filtering ----------------------------------
def test_content_tokens_drops_short_tokens():
    assert content_tokens("ag co grazitti interactive") == ["grazitti", "interactive"]


# --- acronym_pairs: acronym <-> expansion matching -------------------------
def test_acronym_pairs_matches_plain_initialism():
    orgs = [("ibm", 1), ("international business machines", 2), ("acme corp", 3)]
    pairs = acronym_pairs(orgs)
    assert [(p[0], p[2], p[4]) for p in pairs] == [
        ("ibm", "international business machines", "acronym")]


def test_acronym_pairs_matches_stopword_skipping_initialism():
    # 'acm' == initials of a/c/m after dropping the stopword 'for'
    orgs = [("acm", 1), ("association for computing machinery", 2)]
    assert [(p[0], p[2]) for p in acronym_pairs(orgs)] == [
        ("acm", "association for computing machinery")]


def test_acronym_pairs_ignores_single_letters_and_long_tokens():
    # single-letter acronym (<2) and a long single token (>7) are both skipped
    orgs = [("a", 1), ("apple", 2), ("engineering", 3), ("e w corp", 4)]
    assert acronym_pairs(orgs) == []


def test_acronym_pairs_requires_initials_to_match_exactly():
    # 'ibm' does not spell 'international data corporation' (idc) -> no false pair
    orgs = [("ibm", 1), ("international data corporation", 2)]
    assert acronym_pairs(orgs) == []


def test_acronym_pairs_skips_non_alpha_acronym_side():
    # a digit-bearing single token is not treated as an acronym key
    orgs = [("b2b", 1), ("business to business", 2)]
    assert acronym_pairs(orgs) == []


# --- evaluate: recall accounting + failure bucketing -----------------------
def _gold(key_a, key_b, label, **kw):
    return {"key_a": key_a, "key_b": key_b, "label": label,
            "name_a": key_a, "name_b": key_b, "sim": kw.get("sim", 1.0),
            "band": kw.get("band", "high")}


def test_evaluate_found_missing_and_gap_buckets():
    gold = [
        _gold("acme", "acme corp", "merge"),        # will be found
        _gold("foo", "foo inc", "merge"),           # both orgs exist, not surfaced -> gap
        _gold("ghost", "ghost ltd", "merge"),       # 'ghost' not an org -> missing_org
        _gold("x", "y", "distinct"),                # surfaced distinct -> noise
        _gold("p", "q", "distinct"),                # not surfaced
    ]
    candidates = {
        ("acme", "acme corp"): ("trigram",),
        ("x", "y"): ("rare_token",),
    }
    org_keys = {"acme", "acme corp", "foo", "foo inc", "ghost ltd", "x", "y", "p", "q"}
    r = evaluate(gold, candidates, org_keys)

    assert r["gold_merge_pairs"] == 3
    assert r["found"] == 1
    assert r["failures_blocking_gap"] == 1     # foo/foo inc
    assert r["failures_missing_org"] == 1      # ghost not in orgs
    # achievable excludes the missing_org pair: 1 found of 2 achievable = 50%
    assert r["recall_achievable"] == 50.0
    assert round(r["recall_overall"], 1) == 33.3
    assert r["distinct_surfaced"] == 1         # x/y leaked through
    buckets = {(f["key_a"], f["bucket"]) for f in r["failure_list"]}
    assert ("foo", "blocking_gap") in buckets
    assert ("ghost", "missing_org") in buckets


def test_evaluate_marginal_recall_only_counts_sole_blocker():
    gold = [_gold("a", "b", "merge"), _gold("c", "d", "merge")]
    candidates = {
        ("a", "b"): ("trigram",),               # marginal to trigram
        ("c", "d"): ("rare_token", "trigram"),  # caught by both -> not marginal to either
    }
    r = evaluate(gold, candidates, {"a", "b", "c", "d"})
    assert r["found"] == 2
    assert r["marginal_recall"] == {"trigram": 1}
    # total found-by counts BOTH blockers for the pair caught by both
    assert r["found_by_blocker"] == {"trigram": 2, "rare_token": 1}


def test_evaluate_related_label_is_separate_from_merge_and_distinct():
    gold = [
        _gold("a", "b", "merge"),
        _gold("u", "u foundation", "related"),   # related, surfaced
        _gold("p", "q", "related"),              # related, not surfaced
        _gold("x", "y", "distinct"),
    ]
    candidates = {("a", "b"): ("trigram",), ("u", "u foundation"): ("trigram",)}
    r = evaluate(gold, candidates, {"a", "b", "u", "u foundation", "p", "q", "x", "y"})
    # related pairs count toward neither merge recall nor distinct noise
    assert r["gold_merge_pairs"] == 1 and r["distinct_pairs"] == 1
    assert r["related_pairs"] == 2 and r["related_surfaced"] == 1
    # the related pair is NOT a blocking_gap (it was never a merge target)
    assert r["failures_blocking_gap"] == 0


def test_wilson_interval():
    # n=0 -> no information
    assert wilson_interval(0, 0) == (0.0, 1.0)
    # bounds stay inside [0,1] even at extreme rates
    lo, hi = wilson_interval(0, 20)
    assert lo == 0.0 and 0.0 < hi < 0.25
    # the illustrative case from the design discussion: 10/80 ≈ 12.5%, CI ~6–22%
    lo, hi = wilson_interval(10, 80)
    assert abs((10 / 80) - 0.125) < 1e-9
    assert 0.05 < lo < 0.08 and 0.20 < hi < 0.23
    # interval brackets the point estimate and is narrower with more data
    lo1, hi1 = wilson_interval(50, 100)
    lo2, hi2 = wilson_interval(500, 1000)
    assert lo1 < 0.5 < hi1 and (hi2 - lo2) < (hi1 - lo1)


def test_evaluate_component_breakdown():
    gold = [
        {**_gold("a", "b", "merge"), "_component": "pairs.jsonl"},
        {**_gold("c", "d", "merge"), "_component": "supplement.jsonl"},
        {**_gold("u", "u foundation", "related"), "_component": "supplement.jsonl"},
    ]
    candidates = {("a", "b"): ("trigram",)}   # supplement merge c/d is a gap
    r = evaluate(gold, candidates, {"a", "b", "c", "d", "u", "u foundation"})
    comps = r["components"]
    assert comps["pairs.jsonl"] == {"merge": 1, "distinct": 0, "related": 0, "found": 1}
    assert comps["supplement.jsonl"] == {"merge": 1, "distinct": 0, "related": 1, "found": 0}
