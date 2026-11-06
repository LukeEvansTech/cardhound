"""The format badge, the Gixen link and the slab check that drops CGC slabs sold as PSA 10."""

import pytest
from fastapi.testclient import TestClient
from sleeper import config, match, slab, sweep, web
from sleeper.db import DB
from sleeper.web import create_app
from test_web import ROW, FakeBrowser


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    s = config.load()
    db = DB(s.db_path)
    client = TestClient(create_app(s, db))
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    client.post(
        "/api/import",
        json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 1150}}]},
    )
    fixed = {**ROW, "id": "333", "text": "PSA 10 Vegito BT7-118 SPR $700.00 +$5.64 delivery"}
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW, fixed]))
    return s, db, client


def test_tiles_say_auction_or_buy_it_now_and_auctions_link_to_gixen(env):
    _, _, client = env
    html = client.get("/?show=all").text
    assert 'class="fmt fmt-auction">Auction<' in html
    assert 'class="fmt fmt-bin">Buy it now<' in html
    assert html.count("gixen.com/autosnipe.php?itemid=111&amp;maxbid=") == 1  # the auction only
    assert "itemid=333" not in html
    assert "autosnipe.php?itemid=111" in client.get("/deal/111/psa").text


def test_the_gixen_bid_is_the_deal_limit_and_never_zero_or_infinite():
    class V:
        def __init__(self, max_bid):
            self.max_bid = max_bid

    assert web.snipe_bid({"v": V(462.0), "cap": 470, "ship": 5}) == 462
    assert web.snipe_bid({"v": V(None), "cap": 470, "ship": 5.5}) == 464  # ceiling less delivery
    assert web.snipe_bid({"v": V(0.0), "cap": 470}) is None  # no bid keeps it a deal
    assert web.snipe_bid({"v": V(None), "cap": float("inf")}) is None
    assert web.gixen_link("111", None) == "https://www.gixen.com/autosnipe.php?itemid=111"


def test_titles_naming_another_grader_are_not_psa_10():
    w = {"set": "FIN", "who": "Vivi", "q": "vivi psa 10"}
    assert match.keep(w, "psa", "Vivi Ornitier #0570 PSA 10", "")
    assert not match.keep(w, "psa", "Vivi Ornitier #0570 CGC 9 not PSA 10", "")


def test_slab_reads_rule_out_other_graders_and_lower_grades():
    assert (
        slab.wrong(slab.parse('{"grader": "CGC", "grade": "9", "sure": true}')) == "slab is CGC 9"
    )
    assert (
        slab.wrong(slab.parse('{"grader": "PSA", "grade": "9", "sure": true}')) == "slab is PSA 9"
    )
    assert slab.wrong(slab.parse('{"grader": "PSA", "grade": "10", "sure": true}')) == ""
    assert slab.wrong(slab.parse('{"grader": "CGC", "grade": "9", "sure": false}')) == ""
    assert slab.wrong(slab.parse('{"grader": "NONE", "grade": "", "sure": true}')) == ""
    assert slab.parse("no idea") is None


def test_a_cgc_slab_titled_psa_10_is_dropped(env, monkeypatch):
    s, db, _ = env
    s = s.__class__(**{**s.__dict__, "llm_url": "http://llm.invalid"})

    def download(_src, dest):
        dest.write_bytes(b"x" * 2000)
        return dest

    def read(_url, _key, _model, photo):  # listing 111's slab is a CGC 9
        if photo.name.startswith("111"):
            return {"grader": "CGC", "grade": "9", "sure": True}
        return {"grader": "PSA", "grade": "10", "sure": True}

    monkeypatch.setattr(sweep.photos, "download", download)
    monkeypatch.setattr(sweep.slab, "read", read)
    db.run("UPDATE listings SET art = NULL")
    run_id = db.one("SELECT max(id) AS n FROM runs")["n"]
    assert sweep.slab_check(db, s, run_id) == 1
    assert db.one("SELECT reason FROM blocked WHERE item_id = '111'")["reason"] == "slab is CGC 9"
    assert db.one("SELECT active FROM listings WHERE item_id = '111'")["active"] == 0
    assert db.one("SELECT active FROM listings WHERE item_id = '333'")["active"] == 1
    assert sweep.slab_check(db, s, run_id) == 0  # each listing is read once


def test_photos_go_to_the_model_as_jpeg(tmp_path):
    import cv2
    import numpy as np

    png = tmp_path / "photo.jpg"  # PNG bytes under a .jpg name, as eBay sometimes serves
    cv2.imwrite(str(tmp_path / "x.png"), np.zeros((20, 20, 3), np.uint8))
    png.write_bytes((tmp_path / "x.png").read_bytes())
    assert slab.as_jpeg(png)[:3] == b"\xff\xd8\xff"
    junk = tmp_path / "junk.jpg"
    junk.write_bytes(b"not an image")
    assert slab.as_jpeg(junk) is None
