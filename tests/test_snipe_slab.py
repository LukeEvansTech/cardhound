"""The format badge, Gixen snipes and the slab check that drops CGC slabs sold as PSA 10."""

import io
import urllib.parse

import pytest
from fastapi.testclient import TestClient
from test_web import ROW, FakeBrowser

from sleeper import config, gixen, match, slab, sweep
from sleeper.db import DB
from sleeper.web import create_app


@pytest.fixture(autouse=True)
def no_real_gixen(monkeypatch):
    """A test that forgets to fake Gixen fails instead of logging in to the real service."""

    def refuse(*_a, **_k):
        raise AssertionError("tried to reach Gixen")

    monkeypatch.setattr(gixen.urllib.request, "urlopen", refuse)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    monkeypatch.setenv("GIXEN_USERNAME", "luke")
    monkeypatch.setenv("GIXEN_PASSWORD", "pw")
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


def test_tiles_say_auction_or_buy_it_now_and_auctions_can_be_sniped(env):
    _, _, client = env
    html = client.get("/?show=all").text
    assert 'class="fmt fmt-auction">Auction<' in html
    assert 'class="fmt fmt-bin">Buy it now<' in html
    assert html.count('action="/deal/111/psa/gixen"') == 1  # the auction only
    assert 'action="/deal/333/psa/gixen"' not in html


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def test_snipe_goes_to_gixen_and_is_remembered(env, monkeypatch):
    _, db, client = env
    asked = []

    def urlopen(req, timeout=0):
        asked.append(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query))
        return _Answer(b"<br />OK 111 ADDED")

    monkeypatch.setattr(gixen.urllib.request, "urlopen", urlopen)
    r = client.post("/deal/111/psa/gixen", data={"maxbid": "650", "back": "/?show=all"})
    assert r.status_code == 200 and "Gixen snipe set" in r.text
    assert asked[0]["itemid"] == ["111"] and asked[0]["maxbid"] == ["650.00"]
    assert asked[0]["username"] == ["luke"]
    assert db.one("SELECT maxbid FROM snipes WHERE item_id = '111'")["maxbid"] == 650
    assert "Gixen snipe up to $650" in client.get("/?show=all").text


def test_a_refused_snipe_says_why_and_is_not_recorded(env, monkeypatch):
    _, db, client = env
    monkeypatch.setattr(
        gixen.urllib.request,
        "urlopen",
        lambda req, timeout=0: _Answer(b"<html><body>ERROR (111): NON SUBSCRIBER</body></html>"),
    )
    r = client.post("/deal/111/psa/gixen", data={"maxbid": "650"})
    assert "Gixen error 111: NON SUBSCRIBER" in r.text
    assert db.one("SELECT count(*) AS n FROM snipes")["n"] == 0
    with pytest.raises(gixen.GixenError):
        gixen.snipe("luke", "pw", "../x", 10)  # never sends a non-item through


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
