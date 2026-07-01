"""Unit tests for feature extraction — the pure functions only (DB helpers
compute_rare_tokens/persist_features are exercised by `cli features`)."""
from stevie_platform.canonical.features import (
    FEATURE_NAMES, despaced_trigram_similarity, extract_features, length_ratio,
    normalize_for_features, normalized_token_overlap, prefix_overlap,
    shared_rare_token_count, suffix_match, token_jaccard, trigram_similarity,
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


# --- v2 (v1.1 iteration): normalize_for_features + despaced_trigram_similarity
# Each test below is a direct regression check on a v1 frozen-evaluation false
# negative (see gold/scorer_false_negatives_v1.jsonl).

def test_normalize_strips_leading_article():
    assert normalize_for_features("the korea transportation safety authority") \
        == normalize_for_features("korea transportation safety authority")


def test_normalize_strips_articles_anywhere_not_just_leading():
    assert normalize_for_features("a tale of an apple") == "tale of apple"


def test_normalize_ampersand_equals_spelled_out_and():
    assert normalize_for_features("m&m") == normalize_for_features("m and m")


def test_normalize_strips_punctuation():
    assert normalize_for_features("o'brien-smith, inc.") == "o brien smith inc"


def test_normalize_collapses_whitespace_and_is_idempotent():
    once = normalize_for_features("the  acme   corp")
    assert once == "acme corp"
    assert normalize_for_features(once) == once


def test_despaced_trigram_similarity_catches_concatenation():
    # the exact v1 false negative: shares ZERO whitespace tokens, so
    # token_jaccard/normalized_token_overlap are both 0 for this pair.
    assert token_jaccard("rhino runner", "rhinorunner") == 0.0
    assert despaced_trigram_similarity("rhino runner", "rhinorunner") == 1.0


def test_extract_features_recovers_the_article_false_negative():
    feats_v2 = extract_features(
        "the korea transportation safety authority",
        "korea transportation safety authority", ("trigram",))
    # after stripping 'the', these are byte-identical -> perfect similarity and
    # length_ratio, instead of v1's length_ratio=0.905 that the negative
    # coefficient penalized.
    assert feats_v2["trigram_similarity"] == 1.0
    assert feats_v2["token_jaccard"] == 1.0
    assert feats_v2["length_ratio"] == 1.0


def test_extract_features_recovers_the_concatenation_false_negative():
    feats_v2 = extract_features("rhino runner", "rhinorunner", ("trigram",))
    assert feats_v2["token_jaccard"] == 0.0  # unchanged — still a real gap in that feature
    assert feats_v2["despaced_trigram_similarity"] == 1.0  # v2's new signal fills it in


def test_extract_features_set_matches_feature_names_after_v2():
    feats = extract_features("acme corp", "acme corporation", ())
    assert set(feats) == set(FEATURE_NAMES)
    assert "despaced_trigram_similarity" in feats


# --- v3 (v1.2 iteration): acronym_x_trigram / acronym_x_jaccard interactions

def test_interaction_terms_are_zero_for_non_acronym_pairs():
    feats = extract_features("acme corp", "acme corporation", ("trigram",))
    assert feats["is_acronym_expansion"] is False
    assert feats["acronym_x_trigram"] == 0.0
    assert feats["acronym_x_jaccard"] == 0.0


def test_interaction_terms_equal_raw_similarity_for_acronym_pairs():
    feats = extract_features("ibm", "international business machines", ("acronym",))
    assert feats["is_acronym_expansion"] is True
    assert feats["acronym_x_trigram"] == feats["trigram_similarity"]
    assert feats["acronym_x_jaccard"] == feats["token_jaccard"]


def test_interaction_terms_present_in_feature_set():
    feats = extract_features("acme corp", "acme corporation", ())
    assert set(feats) == set(FEATURE_NAMES)
    assert {"acronym_x_trigram", "acronym_x_jaccard"} <= set(feats)
