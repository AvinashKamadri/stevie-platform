from stevie_platform.canonical.normalize import edition_slug, norm_key, slugify


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
