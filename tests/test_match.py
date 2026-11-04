from cardhound import match

EDWARD = {"set": "FMA", "who": "Edward Elric", "max": {"2": 1000}, "rawmax": {"2": 300}}
DEFAULTS = {"psa_1star_max": 80, "raw_1star_max": 20}


def test_stars_buckets():
    assert match.stars("Union Arena FMA Edward 3 Star Serial") == "3"
    assert match.stars("Edward SR** signature UE09BT") == "2"
    assert match.stars("Edward alt art FMA-1-011") == "1"
    assert match.stars("Edward Elric UE09BT common") == "-"


def test_union_arena_searches_use_set_words():
    (k1, q1), (k2, q2) = match.searches(EDWARD)
    assert (k1, k2) == ("psa", "raw")
    assert "psa 10" in q1 and "ue09bt" in q1
    assert q2.startswith("union arena fullmetal alchemist Edward Elric")


def test_free_form_want_without_rawq_has_psa_search_only():
    w = {"set": "DBS", "who": "Vegito", "q": "vegito bt7-118 psa 10"}
    assert match.searches(w) == [("psa", "vegito bt7-118 psa 10")]


def test_keep_needs_union_arena_marker_and_grade():
    t = "PSA 10 Edward Elric 2 Star Union Arena FMA"
    assert match.keep(EDWARD, "psa", t, t)
    assert not match.keep(EDWARD, "psa", "PSA 10 Edward Elric 2 Star", "")
    assert not match.keep(EDWARD, "psa", "Edward Elric 2 Star Union Arena", "")


def test_keep_drops_japanese_and_foreign_sellers():
    t = "PSA 10 Edward Elric Japanese Union Arena"
    assert not match.keep(EDWARD, "psa", t, t)
    t2 = "PSA 10 Edward Elric Union Arena 2 star"
    assert not match.keep(EDWARD, "psa", t2, t2 + " Located in Japan")


def test_raw_must_be_ungraded_and_starred():
    assert match.keep(EDWARD, "raw", "Union Arena Edward Elric 2 star", "")
    assert not match.keep(EDWARD, "raw", "Union Arena Edward Elric 2 star BGS 9.5", "")
    assert not match.keep(EDWARD, "raw", "Union Arena Edward Elric common", "")


def test_dbs_autographs_are_not_the_printed_signature():
    w = {"set": "DBS", "who": "Vegito", "q": "x"}
    assert not match.keep(w, "psa", "Vegito SPR PSA 10 signed by Sabat", "")
    assert match.keep(w, "psa", "Vegito SPR PSA 10 BT7-118", "")


def test_ceilings():
    assert match.ceiling(EDWARD, "psa", "2", DEFAULTS) == 1000
    assert match.ceiling(EDWARD, "psa", "1", DEFAULTS) == 80
    assert match.ceiling(EDWARD, "psa", "3", DEFAULTS) is None
    assert match.ceiling(EDWARD, "raw", "2", DEFAULTS) == 300
    assert match.ceiling(EDWARD, "raw", "1", DEFAULTS) == 20
    assert match.ceiling({"max": {"any": 50}}, "psa", "-", DEFAULTS) == 50


def test_money_and_shipping():
    assert match.money("$1,234.50") == 1234.5
    assert match.money("") == 0.0
    assert match.shipping("Buy It Now +$5.64 delivery Located in United States") == 5.64
    assert match.shipping("Free delivery") == 0.0
    assert match.shipping("Buy It Now") is None
