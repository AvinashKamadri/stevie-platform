"""Person-name extraction tests — pure, no DB. Cases are real nomination titles."""
from stevie_platform.canonical.people_extract import (
    extract_person, parse_title, person_key, resolve_people,
)


def test_name_comma_title():
    assert extract_person("Ines Ruiz, CEO and Founder") == "Ines Ruiz"
    assert extract_person("Kimberly Khoury, Chief Commercial Officer") == "Kimberly Khoury"
    assert extract_person("Ed McLaughlin, President and Chief Technology Officer") == "Ed McLaughlin"
    assert extract_person("Michael Nadeau, President & CEO") == "Michael Nadeau"


def test_clean_name():
    assert extract_person("Silvija Martincevic") == "Silvija Martincevic"
    assert extract_person("Hannah Kain") == "Hannah Kain"


def test_colon_and_dash_blurbs():
    assert extract_person("Terri Todd: Pioneering Transformative Change") == "Terri Todd"
    assert extract_person("Christy McCarly - All Clear ID Fraud Investigator") == "Christy McCarly"


def test_honorifics_and_parens():
    assert extract_person("Mr. Brian Ippolito, CEO") == "Brian Ippolito"
    assert extract_person("Miss Syeda Amna Nasir Jamal (Chair Person)") == "Syeda Amna Nasir Jamal"


def test_apostrophes_and_hyphens():
    assert extract_person("Maryanne O'Neill, Sr. Manager, Support Services") == "Maryanne O'Neill"
    assert extract_person("Amy Cappellanti-Wolf, Chief Human Resources Officer") == "Amy Cappellanti-Wolf"


def test_drops_allcaps_certs():
    assert extract_person("Barry Tourigny SPHR GPHR, VP of Human Resources") == "Barry Tourigny"


def test_unicode_names():
    assert extract_person("José Manuel Martinez, President") == "José Manuel Martinez"
    assert extract_person("Tesa Díaz-Faes, Chief Communications Officer") == "Tesa Díaz-Faes"


def test_keeps_middle_initial():
    assert extract_person("Steven T. Plochocki, Healthcare Veteran") == "Steven T. Plochocki"


def test_rejects_orgs():
    assert extract_person("NLP Alliance Japan, Osaka, Japan") is None


def test_rejects_narrative_and_project_titles():
    assert extract_person("Leadership under impossible standards") is None
    assert extract_person("Quick study delivers wow customer service for clients") is None
    assert extract_person("Driven and Determined: How Courtney Moore is changing the game") is None
    assert extract_person("Innovative initiatives and projects that are transformative") is None


def test_empty():
    assert extract_person("") is None
    assert extract_person(None) is None


def test_person_key_normalizes():
    assert person_key("Ed McLaughlin") == person_key("ed mclaughlin")
    assert person_key("Miss Syeda Amna Nasir Jamal") != ""


def test_parse_title():
    assert parse_title("Ines Ruiz, CEO and Founder") == "CEO and Founder"
    assert parse_title("Hannah Kain") is None
    assert parse_title("X, " + " ".join(["word"] * 15)) is None   # too long -> None


def test_resolve_dedups_same_name_same_employer():
    recs = [
        {"rec_id": 1, "nomination_title": "Nicole McMackin, President", "org_id": 10},
        {"rec_id": 2, "nomination_title": "Nicole McMackin, President", "org_id": 10},
        {"rec_id": 3, "nomination_title": "Leadership under pressure", "org_id": 10},  # not a name
    ]
    people = resolve_people(recs)
    assert len(people) == 1
    assert sorted(people[0]["rec_ids"]) == [1, 2]
    assert people[0]["confidence"] == 0.68            # 2 corroborating recs


def test_resolve_splits_same_name_different_employer():
    recs = [
        {"rec_id": 1, "nomination_title": "John Smith, CEO", "org_id": 10},
        {"rec_id": 2, "nomination_title": "John Smith, CFO", "org_id": 20},
    ]
    people = resolve_people(recs)
    assert len(people) == 2
    slugs = {p["slug"] for p in people}
    assert slugs == {"john-smith", "john-smith-2"}     # unique slugs
