import datetime as dt

from cardhound import value

TODAY = dt.date(2026, 11, 4)
W = {"_id": 1, "set": "FMA", "who": "Edward Elric"}


def sold(kind, star, *prices):
    return [
        {"want_id": 1, "kind": kind, "star": star, "usd": p, "date": "2026-10-20"} for p in prices
    ]


def test_comps_window_and_median():
    rows = sold("psa", "2", 100, 200, 300) + [
        {"want_id": 1, "kind": "psa", "star": "2", "usd": 999, "date": "2026-01-01"}
    ]
    c = value.comps(rows, TODAY)[(1, "psa", "2")]
    assert (c.median, c.count, c.low, c.high) == (200, 3, 100, 300)


def listing(kind="psa", usd=150.0, ship=0.0, cap=1000.0, auction=0):
    return {"kind": kind, "star": "2", "usd": usd, "ship": ship, "cap": cap, "auction": auction}


def test_slab_below_median_is_great_buy():
    cmp = value.comps(sold("psa", "2", 200, 210, 220), TODAY)
    v = value.assess(listing(usd=140), W, cmp, [], {"FMA": 0.77}, 39.99, 6)
    assert v.rating == "great"
    assert "Buy the slab" in v.advice


def test_slab_where_grading_is_much_cheaper_says_so():
    cmp = value.comps(sold("psa", "2", 1000, 1100, 1200) + sold("raw", "2", 150, 160), TODAY)
    v = value.assess(listing(usd=1050), W, cmp, [], {"FMA": 0.77}, 39.99, 6)
    assert v.grade_cost is not None and v.grade_cost < 300
    assert "raw and grading is cheaper" in v.advice


def test_raw_to_grade_uses_card_pop_when_enough_graded():
    pop = [{"descr": "Edward Elric 2 Star 012", "gems": 5, "total": 10, "set_name": "x"}]
    cmp = value.comps(sold("psa", "2", 1000), TODAY)
    v = value.assess(listing(kind="raw", usd=200), W, cmp, pop, {"FMA": 0.77}, 39.99, 6)
    assert round(v.grade_cost) == round((200 + 39.99 + 6) / 0.5)
    assert "Buy to grade" in v.advice
    assert any("this card's PSA pop (5/10)" in r for r in v.reasons)


def test_no_history_under_ceiling_is_good_not_unknown():
    v = value.assess(listing(usd=50, cap=80), W, {}, [], {}, 39.99, 6)
    assert v.rating == "good"


def test_auction_gets_a_max_bid_under_ceiling_and_median():
    cmp = value.comps(sold("psa", "2", 500), TODAY)
    v = value.assess(listing(usd=100, ship=10, cap=400, auction=1), W, cmp, [], {}, 39.99, 6)
    assert v.max_bid == 390


def test_gem_rate_stays_in_its_star_bucket():
    pop = [
        {"descr": "Edward Elric 2 Star 012", "gems": 1, "total": 10, "set_name": "x"},
        {"descr": "Edward Elric 012", "gems": 99, "total": 100, "set_name": "x"},
    ]
    rate, _ = value.gem_rate(W, "2", pop, {})
    assert rate == 0.1


def test_max_bid_stays_a_deal_at_the_new_cutoff():
    cmp = value.comps(sold("psa", "2", 100), TODAY)
    v = value.assess(listing(usd=50, ship=0, cap=100, auction=1), W, cmp, [], {}, 39.99, 6)
    assert v.max_bid == 92
    at_bid = value.assess(listing(usd=v.max_bid, cap=100), W, cmp, [], {}, 39.99, 6)
    assert at_bid.is_deal
    raw = value.comps(sold("raw", "2", 50), TODAY)
    v = value.assess(listing(kind="raw", usd=10, cap=60, auction=1), W, raw, [], {}, 39.99, 6)
    assert v.max_bid == 46
