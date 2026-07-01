"""Unit tests for M5.2 feature extraction — the pure functions only (DB helpers
compute_rare_tokens/persist_features are exercised by `cli features`)."""
from stevie_platform.canonical.features import (
    FEATURE_NAMES, extract_features, length_ratio, normalized_token_overlap,
    prefix_overlap, shared_rare_token_count, suffix_match, token_jaccard,
    trigram_similarity,
)


def test_trigram_similarity_identical_is_one():
    assert trigram_similarity("acme corp", "acme corp") == 1.0


def test_trigram_similarity_disjoint_is_zero():
    assert trigram_similarity("acme corp", "zzz xyz qqq") == 0.0


def test_trigram_similarity_low_for_acronym_pair():
    # this is the whole reason the acronym blocker exists: trigram sees ~nothing
    sim = trigram_similarity("ibm", "international business machines")
    assert sim < 0.1


def test_token_jaccard_partial_overlap():
    assert token_jaccard("acme global", "acme corp") == 1 / 3  # {acme} / {acme,global,corp}


def test_length_ratio_symmetric_and_bounded():
    assert length_ratio("ab", "abcd") == 0.5
    assert length_ratio("abcd", "ab") == 0.5
    assert length_ratio("", "abc") == 0.0


def test_normalized_token_overlap_subset_is_high():
    # 'acme' tokens are a strict subset of the other's -> overlap coefficient 1.0
    # even though jaccard would be lower (union includes the extra tokens).
    assert normalized_token_overlap("acme", "acme global holdings") == 1.0
    assert token_jaccard("acme", "acme global holdings") == 1 / 3


def test_shared_rare_token_count():
    rare = frozenset({"grazitti"})
    assert shared_rare_token_count("grazitti interactive", "grazitti media", rare) == 1
    assert shared_rare_token_count("acme corp", "acme media", rare) == 0


def test_prefix_and_suffix_overlap():
    assert prefix_overlap("international", "internal") > 0.5
    assert prefix_overlap("acme", "zzzz") == 0.0
    assert suffix_match("data systems", "info systems") > 0.0
    assert suffix_match("acme", "zzzz") == 0.0


def test_extract_features_returns_exactly_the_named_vector():
    feats = extract_features("ibm", "international business machines", ("acronym",))
    assert set(feats) == set(FEATURE_NAMES)
    assert feats["blocked_by_acronym"] is True
    assert feats["blocked_by_trigram"] is False
    assert feats["blocked_by_rare_token"] is False
    assert feats["is_acronym_expansion"] is True
    assert feats["trigram_similarity"] < 0.1  # acronym pairs are trigram-invisible


def test_extract_features_reasons_are_independent_flags():
    # a pair found by BOTH trigram and rare_token sets both flags
    feats = extract_features("acme corp", "acme corporation", ("trigram", "rare_token"))
    assert feats["blocked_by_trigram"] is True
    assert feats["blocked_by_rare_token"] is True
    assert feats["blocked_by_acronym"] is False


def test_extract_features_is_symmetric_in_key_order():
    a = extract_features("acme corp", "acme corporation", ("trigram",))
    b = extract_features("acme corporation", "acme corp", ("trigram",))
    assert a == b
