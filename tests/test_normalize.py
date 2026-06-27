from stevie_platform.canonical.normalize import (
    build_location_vocab, edition_slug, location_dedup_key,
    location_display_name, norm_key, slugify,
)

_VOCAB = build_location_vocab(["United States", "Singapore", "Australia",
                               "Germany", "Turkey", "Philippines"])


def _lk(name, **kw):
    return location_dedup_key(name, vocab=_VOCAB, **kw)


def test_norm_key_strips_accents_marks_punct():
    assert norm_key("Türkiye") == "turkiye"
    assert norm_key("Gold Stevie® Award") == "gold stevie award"
    assert norm_key("IBM Corp.") == "ibm corp"
    assert norm_key("  AT&T   Inc ") == "at t inc"


def test_norm_key_collapses_safe_equivalents():
    # case + spacing variants dedup exactly...
    assert norm_key("IBM") == norm_key("  ibm ") == "ibm"


def test_norm_key_is_conservative():
    # ...but "I.B.M." -> "i b m" must NOT collapse into "ibm". Exact match stays
    # conservative; the IBM/I.B.M. equivalence is for Phase C candidates to flag,
    # not for norm_key to force (silent over-merging is the worse failure).
    assert norm_key("I.B.M.") == "i b m"
    assert norm_key("I.B.M.") != norm_key("IBM")


def test_norm_key_keeps_non_latin():
    # must NOT collapse to empty (would collide every CJK name into one row)
    assert norm_key("株式会社") != ""


def test_norm_key_empty():
    assert norm_key("") == "" and norm_key(None) == ""


def test_slugify():
    assert slugify("Türkiye") == "turkiye"
    assert slugify("AI & Customer Service") == "ai-customer-service"
    assert slugify("") == "n-a"


def test_edition_slug():
    assert edition_slug("The International Business Awards", 2024) == \
        "the-international-business-awards-2024"


# --- location rule -----------------------------------------------------------

def test_location_rule_collapses_city_state_country_variants():
    assert _lk("IBM") == "ibm"
    assert _lk("IBM, Armonk, NY", city="Armonk", state="NY",
               country="United States") == "ibm"
    assert _lk("IBM, Armonk, NY, USA", city="Armonk", state="NY",
               country="United States") == "ibm"
    assert _lk("Accenture, Chicago, Illinois", city="Chicago",
               state="Illinois") == "accenture"


def test_location_rule_strips_country_alias_mismatch():
    # record country "US" but name says "United States" -> still stripped
    assert _lk("Fortinet, Sunnyvale, United States", city="Sunnyvale") == "fortinet"


def test_location_rule_keeps_corporate_suffix():
    # location rule must NOT touch legal suffixes (that's the held rule)
    assert _lk("Cisco Systems, Inc., San Jose, CA", city="San Jose",
               state="CA") == "cisco systems inc"


def test_location_rule_never_truncates_real_name_or_empties():
    assert _lk("Delta Air Lines, Asia Pacific") == "delta air lines asia pacific"
    assert _lk("NY") == "ny"          # a name that is only a location token


def test_location_display_name_is_clean():
    assert location_display_name("IBM, Armonk, NY", city="Armonk", state="NY",
                                 country="United States", vocab=_VOCAB) == "IBM"
    assert location_display_name("Cisco Systems, Inc., San Jose, CA",
                                 city="San Jose", state="CA",
                                 vocab=_VOCAB) == "Cisco Systems, Inc."
