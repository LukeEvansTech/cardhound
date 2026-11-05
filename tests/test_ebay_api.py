"""Browse API client against a fake HTTP layer: rows must read like the website's search cards."""

import datetime as dt
import json
import urllib.parse

from cardhound import config, match, sweep
from cardhound.ebay_api import BrowseAPI, row, split_query, time_left

NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)


def item(n, title, **kw):
    out = {
        "itemId": f"v1|{n}|0",
        "legacyItemId": str(n),
        "title": title,
        "price": {"value": "25.00", "currency": "USD"},
        "buyingOptions": ["FIXED_PRICE"],
        "shippingOptions": [{"shippingCost": {"value": "4.50", "currency": "USD"}}],
        "itemLocation": {"country": "US"},
        "image": {"imageUrl": f"https://i.ebayimg.com/images/g/{n}/s-l225.jpg"},
    }
    out.update(kw)
    return out


class FakeHTTP:
    def __init__(self, pages, statuses=None):
        self.pages, self.statuses, self.calls = pages, list(statuses or []), []

    def __call__(self, req):
        url = req.full_url
        self.calls.append(req)
        if "oauth2/token" in url:
            return 200, json.dumps({"access_token": "tok", "expires_in": 7200}).encode()
        if self.statuses:
            return self.statuses.pop(0), b"{}"
        if "get_item_by_legacy_id" in url:
            return (
                200,
                json.dumps(
                    {
                        "image": {"imageUrl": "https://i.ebayimg.com/images/g/a/s-l500.jpg"},
                        "additionalImages": [
                            {"imageUrl": "https://i.ebayimg.com/images/g/b/s-l500.jpg"},
                            {"imageUrl": "https://i.ebayimg.com/images/g/a/s-l500.jpg"},
                        ],
                    }
                ).encode(),
            )
        offset = int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["offset"])
        return 200, json.dumps(self.pages[offset // 200]).encode()


def api(http):
    return BrowseAPI("id", "secret", pause=0, fetch=http)


def test_split_query_moves_exclusions_out():
    q, drop = split_query("union arena sr aki -psa -bgs -cgc -tag")
    assert q == "union arena sr aki"
    assert drop == ["psa", "bgs", "cgc", "tag"]


def test_row_reads_like_a_search_card():
    end = (NOW + dt.timedelta(days=1, hours=4, minutes=5)).isoformat()
    r = row(
        item(
            7,
            "Aki SR 2 star",
            buyingOptions=["AUCTION", "BEST_OFFER"],
            currentBidPrice={"value": "12.00", "currency": "USD"},
            bidCount=3,
            itemEndDate=end,
            itemLocation={"country": "JP"},
        ),
        NOW,
    )
    assert r is not None
    assert r["id"] == "7" and r["price"] == "$12.00"
    assert match.shipping(r["text"]) == 4.5
    assert "3 bids" in r["text"] and "1d 4h left" in r["text"] and "or Best Offer" in r["text"]
    assert match.FOREIGN_SELLER.search(r["text"])  # a Japanese seller is dropped as on the site


def test_row_free_delivery_and_missing_price():
    free = item(8, "x", shippingOptions=[{"shippingCost": {"value": "0.00"}}])
    got = row(free)
    assert got is not None
    assert match.shipping(got["text"]) == 0.0
    assert row(item(9, "x", price=None)) is None


def test_time_left_wording():
    assert time_left((NOW + dt.timedelta(minutes=95)).isoformat(), NOW) == "1h 35m left"
    assert time_left((NOW - dt.timedelta(minutes=1)).isoformat(), NOW) == ""


def test_search_pages_filters_and_sends_location():
    pages = [
        {"total": 250, "itemSummaries": [item(i, f"Aki SR {i}") for i in range(200)]},
        {"total": 250, "itemSummaries": [item(500, "Aki SR PSA 10"), item(501, "Aki SR raw")]},
    ]
    http = FakeHTTP(pages)
    rows = api(http).search(match.search_url("union arena aki -psa"))
    assert len(rows) == 201  # the PSA title is dropped by the -psa exclusion
    search = [c for c in http.calls if "item_summary" in c.full_url]
    assert len(search) == 2
    assert "zip%3D97035" in search[0].get_header("X-ebay-c-enduserctx")
    assert "-psa" not in search[0].full_url


def test_sold_search_is_empty_without_a_call():
    http = FakeHTTP([])
    assert api(http).search(match.search_url("aki", sold=True)) == []
    assert not http.calls


def test_retries_then_gives_up():
    http = FakeHTTP([{"total": 0, "itemSummaries": []}], statuses=[500, 429])
    assert api(http).search(match.search_url("aki")) == []
    assert api(FakeHTTP([], statuses=[500] * 4)).search(match.search_url("aki")) is None


def test_item_photos_full_size_and_unique():
    assert api(FakeHTTP([])).item_photos("7") == [
        "https://i.ebayimg.com/images/g/a/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/b/s-l1600.jpg",
    ]


def test_keyset_switches_the_sweep_to_the_api(tmp_path, monkeypatch):
    monkeypatch.setenv("CARDHOUND_DATA", str(tmp_path))
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    assert not isinstance(sweep.default_source(config.load()), BrowseAPI)
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    assert isinstance(sweep.default_source(config.load()), BrowseAPI)
