"""Lock the experimental normalization behavior — deterministic + auditable.

    .venv/bin/python -m pytest experiments/org_normalization/test_normalize_v2.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from normalize_v2 import base_location_vocab, enhanced_key, strip_location_clause

VOCAB = base_location_vocab(["United States", "Singapore", "Australia",
                             "Germany", "Turkey", "Philippines"])


def ek(name, **kw):
    return enhanced_key(name, base_vocab=VOCAB, **kw)


def test_location_variants_collapse_to_core():
    assert ek("IBM") == "ibm"
    assert ek("IBM, Armonk, NY", city="Armonk", state="NY",
              country="United States") == "ibm"
    assert ek("IBM, Armonk, NY, USA", city="Armonk", state="NY",
              country="United States") == "ibm"


def test_state_name_and_abbrev_both_stripped():
    assert ek("Accenture, Chicago, IL", city="Chicago", state="IL") == "accenture"
    assert ek("Accenture, Chicago, Illinois", city="Chicago",
              state="Illinois") == "accenture"


def test_country_alias_stripped():
    assert ek("Fortinet, Sunnyvale, US", city="Sunnyvale") == "fortinet"
    assert ek("Fortinet, Sunnyvale, United States", city="Sunnyvale") == "fortinet"


def test_redundant_trailing_country_token_in_name_collapses():
    assert ek("Nu Skin Enterprises Singapore, Singapore",
              country="Singapore") == "nu skin enterprises singapore"


def test_real_name_segment_is_never_truncated():
    # "Asia" is not a location token here -> kept (conservative).
    assert ek("Delta Air Lines, Asia Pacific") == "delta air lines asia pacific"


def test_non_location_trailing_segment_stops_stripping():
    # Stops at the first non-location segment from the right.
    assert strip_location_clause("Foo, Bar Division, NY", city=None, state="NY",
                                 country=None, base_vocab=VOCAB) == "Foo, Bar Division"


def test_corporate_suffix_is_NOT_stripped_yet():
    # Deliberately conservative: Inc/LLC are a separate, riskier rule.
    assert ek("Cisco Systems") != ek("Cisco Systems Inc")


def test_never_collapses_a_real_name_to_empty():
    assert ek("NY") == "ny"          # a name that is only a location token
    assert ek("USA") == "usa"


def test_single_segment_unchanged():
    assert ek("Tata Consultancy Services") == "tata consultancy services"
