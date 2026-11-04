"""End to end: import wants, sweep with a fake browser, open the alert's deal page."""

import pytest
from cardhound import config, sweep
from cardhound.db import DB
from cardhound.web import create_app
from fastapi.testclient import TestClient


class FakeBrowser:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def search(self, url):
        return [] if "LH_Sold" in url else self.rows

    def item_photos(self, _item_id):
        return []


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CARDHOUND_DATA", str(tmp_path))
    monkeypatch.setenv("CARDHOUND_PUBLIC_URL", "https://cardhound.example")
    s = config.load()
    db = DB(s.db_path)
    return s, db, TestClient(create_app(s, db))


ROW = {
    "id": "111",
    "title": "PSA 10 Vegito BT7-118 SPR Dragon Ball Super",
    "price": "$700.00",
    "text": "PSA 10 Vegito BT7-118 SPR $700.00 +$5.64 delivery 3 bids 1d 2h left",
    "img": "https://i.ebayimg.com/images/g/x/s-l140.jpg",
}


def test_import_sweep_and_deal_page(env, monkeypatch):
    s, db, client = env
    sent = []
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: sent.append(a) or "sent")
    r = client.post(
        "/api/import",
        json={
            "wants": [{"set": "DBS", "who": "Vegito", "q": "vegito psa 10", "max": {"any": 1150}}],
            "pop": {"fetched": "2026-11-04", "rows": []},
        },
    )
    assert r.json()["added"] == 1
    # first sweep only records a baseline: one summary alert, nothing per deal
    run1 = sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert db.one("SELECT status FROM runs WHERE id = ?", run1)["status"] == "success"
    assert len(sent) == 1 and "watching" in sent[0][1]
    # a new listing on the next sweep gets its own alert linking to its deal page
    row2 = {**ROW, "id": "222", "price": "$650.00"}
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW, row2]))
    assert len(sent) == 2
    assert sent[1][3] == "https://cardhound.example/deal/222/psa"
    page = client.get("/deal/222/psa")
    assert page.status_code == 200
    assert "Vegito" in page.text and "$656" in page.text
    assert "snipe up to" in page.text
    assert client.get("/").status_code == 200
    assert client.get(f"/run/{run1}").status_code == 200
    assert client.get("/wants").status_code == 200
    assert client.get("/ops").status_code == 200
    assert "cardhound_last_success_timestamp_seconds" in client.get("/metrics").text


def test_failed_send_is_retried_next_sweep(env, monkeypatch):
    s, db, client = env
    client.post("/api/import", json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v"}]})
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    sweep.run(db, s, browser=lambda: FakeBrowser([]))  # baseline with nothing live
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "failed: x")
    db.run("UPDATE wants SET spec = json_set(spec, '$.max', json('{\"any\": 900}'))")
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert db.one("SELECT count(*) AS n FROM alerts")["n"] == 0
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert db.one("SELECT count(*) AS n FROM alerts")["n"] == 1


def test_want_form_validates(env):
    _, _, client = env
    r = client.post("/wants", data={"who": "Cloud", "set": "FIN", "extra": "{}", "enabled": "on"})
    assert "need an eBay search" in r.text
    r = client.post(
        "/wants",
        data={
            "who": "Cloud",
            "set": "FIN",
            "q": "cloud surge foil",
            "max_any": "$45",
            "extra": "{}",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get(r.headers["location"]).status_code == 200


def test_ref_upload_rejects_path_tricks(env):
    _, _, client = env
    assert client.put("/api/refs/..%2Fx.jpg", content=b"x" * 2000).status_code in (400, 404)
    assert client.put("/api/refs/ok.jpg", content=b"x" * 2000).status_code == 200
    assert client.get("/refs/ok.jpg").status_code == 200


def test_sold_import_maps_wants_and_feeds_verdict(env):
    _, db, client = env
    client.post("/api/import", json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v"}]})
    sold = {
        "9": {
            "set": "DBS",
            "who": "Vegito",
            "kind": "psa",
            "star": "-",
            "usd": 1188,
            "date": "2099-01-01",
            "title": "Vegito PSA 10",
        },
        "8": {
            "set": "DBS",
            "who": "Nobody",
            "kind": "psa",
            "star": "-",
            "usd": 5,
            "date": "2099-01-01",
            "title": "x",
        },
    }
    assert client.post("/api/sold", json={"sold": sold}).json() == {"added": 1}
    assert client.post("/api/sold", json={"sold": sold}).json() == {"added": 0}
    assert db.one("SELECT usd FROM sold")["usd"] == 1188


def test_failed_baseline_send_is_retried(env, monkeypatch):
    s, db, client = env
    client.post(
        "/api/import",
        json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 900}}]},
    )
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "failed: x")
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert db.one("SELECT count(*) AS n FROM alerts")["n"] == 0
    sent = []
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: sent.append(a) or "sent")
    sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    assert len(sent) == 1 and "watching" in sent[0][1]
    assert db.one("SELECT count(*) AS n FROM alerts")["n"] == 1


def test_sweep_with_no_wants_is_not_a_success(env):
    s, db, _ = env
    run_id = sweep.run(db, s, browser=lambda: FakeBrowser([]))
    assert db.one("SELECT status FROM runs WHERE id = ?", run_id)["status"] == "failed"
    assert not db.get("baseline_done", False)


def test_unticked_watching_pauses_and_delete_clears_sales(env):
    _, db, client = env
    r = client.post(
        "/wants",
        data={"who": "Cloud", "set": "FIN", "q": "cloud", "extra": "{}"},
        follow_redirects=False,
    )
    want_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert db.want(want_id)["_enabled"] is False
    client.post(
        "/api/sold",
        json={
            "sold": {
                "1": {
                    "set": "FIN",
                    "who": "Cloud",
                    "kind": "psa",
                    "star": "-",
                    "usd": 10,
                    "date": "2099-01-01",
                    "title": "t",
                }
            }
        },
    )
    client.post(f"/wants/{want_id}/delete")
    assert db.one("SELECT count(*) AS n FROM sold")["n"] == 0
    r = client.post(
        "/wants",
        data={"who": "Tifa", "set": "FIN", "q": "t", "extra": "{}"},
        follow_redirects=False,
    )
    assert int(r.headers["location"].rsplit("/", 1)[1]) != want_id


def test_form_error_keeps_values(env):
    _, _, client = env
    r = client.post(
        "/wants",
        data={
            "who": "Cloud",
            "set": "FIN",
            "q": "c",
            "id": "(",
            "max_any": "45",
            "refs": "cloud.jpg",
            "extra": "{}",
        },
    )
    assert "not a valid regular expression" in r.text
    assert 'value="45"' in r.text and 'value="cloud.jpg"' in r.text


def test_run_page_keeps_its_deals_after_a_later_price_drop(env, monkeypatch):
    s, db, client = env
    client.post(
        "/api/import",
        json={"wants": [{"set": "DBS", "who": "Vegito", "q": "v", "max": {"any": 900}}]},
    )
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    run1 = sweep.run(db, s, browser=lambda: FakeBrowser([ROW]))
    sweep.run(db, s, browser=lambda: FakeBrowser([{**ROW, "price": "$500.00"}]))
    assert "Vegito" in client.get(f"/run/{run1}").text
