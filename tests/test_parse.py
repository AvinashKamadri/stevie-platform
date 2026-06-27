"""Parser unit tests — pure, no network, no DB.

The pipeline's trust rests here: feed saved HTML, assert the structured record.
"""
from stevie_platform.parsing.parse import (
    derive_result_level, is_complete_record, listing_has_captcha,
    parse_detail, parse_listing_ids, parse_total, solve_math,
)

LISTING_HTML = """
<table class="views-table"><tbody>
  <tr><td><a class="a-view-past-winner-details" rel="86016">View</a></td></tr>
  <tr><td><a class="a-view-past-winner-details" rel="86017">View</a></td></tr>
  <tr><td><a class="a-view-past-winner-details" rel="notanid">View</a></td></tr>
</tbody></table>
<div>Displaying 1 - 60 of 82,654</div>
"""

DETAIL_HTML = """
<h5>Hasata - True to Seed, Authentic Taste - by Tiryaki Agro</h5>
<table><tr><th>Organization Name:</th><td>Turkiye Sigorta</td></tr>
<tr><th>Year:</th><td>2024</td></tr>
<tr><th>Award:</th><td>Gold Stevie&reg; Award</td></tr>
<tr><th>Award Programs:</th><td>The International Business Awards</td></tr>
<tr><th>Country:</th><td>Türkiye</td></tr>
<tr><th>Submitting Agency:</th><td>Linkus PR</td></tr></table>
"""


def test_parse_total():
    assert parse_total(LISTING_HTML) == 82654


def test_solve_math():
    assert solve_math("Math question 17 + 3 =") == 20
    assert solve_math("9 - 4 =") == 5
    assert solve_math("6 x 7 =") == 42
    assert solve_math("no numbers here") is None


def test_listing_has_captcha():
    assert listing_has_captcha('<input name="captcha_response">') is True
    assert listing_has_captcha(LISTING_HTML) is False


def test_parse_listing_ids():
    assert parse_listing_ids(LISTING_HTML) == ["86016", "86017"]


def test_parse_detail():
    rec = parse_detail(DETAIL_HTML, "86016")
    assert rec["node_id"] == "86016"
    assert rec["organization_name"] == "Turkiye Sigorta"
    assert rec["year"] == "2024"
    assert rec["country"] == "Türkiye"
    assert rec["submitting_agency"] == "Linkus PR"
    assert rec["result_level"] == "gold"
    assert "Hasata" in rec["nomination_title"]


def test_derive_result_level():
    assert derive_result_level("Gold Stevie Award") == "gold"
    assert derive_result_level("Finalist") == "finalist"
    assert derive_result_level("") == "other"


def test_is_complete_record():
    assert is_complete_record({"organization_name": "X", "year": "2024", "award": "Gold"}) is True
    assert is_complete_record({"organization_name": "X", "year": "2024"}) is False
