"""One sweep: search eBay for every enabled want, record listings and sales, check photos, alert.

Ledger rule: a listing is marked alerted only after its alert was sent, so a failed send is retried
on the next sweep instead of being lost.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
from typing import Callable

from . import match, notify, photos, value
from .config import Settings
from .db import DB
from .ebay import Browser

log = logging.getLogger(__name__)
LOCK = threading.Lock()
ITEM_PAGES_PER_RUN = 25


def _sold_date(text: str, today: str) -> str:
    m = re.search(r"Sold\s+([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
    if not m:
        return today
    return dt.datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()


def collect(b: Browser, w: dict, blocked: set[str], defaults: dict, today: str, sold_too: bool):
    """(listings, sold, ok_kinds, failed_kinds) for one want."""
    found, sold, ok, failed = [], [], [], []
    for kind, q in match.searches(w):
        rows = b.search(match.search_url(q))
        if rows is None:
            failed.append(kind)
            continue
        ok.append(kind)
        for r in (b.search(match.search_url(q, sold=True)) if sold_too else None) or []:
            if r["id"] in blocked or not match.keep(w, kind, r["title"], r["text"]):
                continue
            sold.append(
                {
                    "item_id": r["id"],
                    "want_id": w["_id"],
                    "kind": kind,
                    "star": match.stars(r["title"]),
                    "usd": match.money(r["price"]),
                    "date": _sold_date(r["text"], today),
                    "title": r["title"][:200],
                }
            )
        for r in rows:
            if r["id"] in blocked or not match.keep(w, kind, r["title"], r["text"]):
                continue
            star = match.stars(r["title"])
            usd = match.money(r["price"])
            ship = match.shipping(r["text"])
            cap = match.ceiling(w, kind, star, defaults)
            bids = re.search(r"\d+ bids?", r["text"])
            left = re.search(r"\d+[dhm] \d+[hm] left", r["text"])
            found.append(
                {
                    "item_id": r["id"],
                    "kind": kind,
                    "want_id": w["_id"],
                    "title": r["title"][:200],
                    "usd": usd,
                    "ship": ship,
                    "star": star,
                    "auction": int(bool(bids)),
                    "bids": bids.group(0) if bids else "",
                    "time_left": left.group(0) if left else "",
                    "obo": int("or Best Offer" in r["text"]),
                    "img": r.get("img", ""),
                    "cap": cap,
                    "deal": int(cap is not None and usd + (ship or 0) <= cap),
                }
            )
    return found, sold, ok, failed


def store(db: DB, run_id: int, found: list[dict], sold: list[dict], searched: list[tuple]) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    with db.tx() as c:
        for s in sold:
            c.execute(
                "INSERT OR IGNORE INTO sold (item_id, want_id, kind, star, usd, date, title) "
                "VALUES (:item_id, :want_id, :kind, :star, :usd, :date, :title)",
                s,
            )
        for f in found:
            c.execute(
                """INSERT INTO listings (item_id, kind, want_id, title, usd, ship, star, auction, bids,
                       time_left, obo, img, cap, deal, first_seen, last_seen, first_run, last_run,
                       low_usd, active)
                   VALUES (:item_id, :kind, :want_id, :title, :usd, :ship, :star, :auction, :bids,
                       :time_left, :obo, :img, :cap, :deal, :now, :now, :run, :run, :usd, 1)
                   ON CONFLICT(item_id, kind) DO UPDATE SET
                       want_id = excluded.want_id, title = excluded.title, usd = excluded.usd,
                       ship = excluded.ship, star = excluded.star, auction = excluded.auction,
                       bids = excluded.bids, time_left = excluded.time_left, obo = excluded.obo,
                       img = excluded.img, cap = excluded.cap, deal = excluded.deal,
                       last_seen = excluded.last_seen, last_run = excluded.last_run,
                       low_usd = min(listings.low_usd, excluded.usd), active = 1""",
                {**f, "now": now, "run": run_id},
            )
        for want_id, kind in searched:
            c.execute(
                "UPDATE listings SET active = 0 WHERE want_id = ? AND kind = ? AND last_run != ?",
                (want_id, kind, run_id),
            )


def photo_check(
    db: DB, s: Settings, b: Browser | None, wants: dict[int, dict], run_id: int
) -> None:
    """Score each live deal's photo against the official art, once per listing."""
    rows = db.all(
        "SELECT item_id, kind, want_id, img, art FROM listings WHERE active = 1 AND deal = 1 "
        "AND last_run = ?",
        run_id,
    )
    pages = ITEM_PAGES_PER_RUN
    for r in rows:
        w = wants.get(r["want_id"])
        if not w or (r["art"] and json.loads(r["art"]).get("verdict") != "unchecked"):
            continue
        res = photos.check(w, r["item_id"], r["img"], s.refs_dir, s.photos_dir)
        if res["verdict"] in ("differs", "unclear") and b is not None and pages > 0:
            pages -= 1
            for i, src in enumerate(b.item_photos(r["item_id"])[:4]):
                alt = photos.check(w, f"{r['item_id']}_{i}", src, s.refs_dir, s.photos_dir)
                if (alt["score"] or 0) > (res["score"] or 0):
                    res = alt
                if alt["verdict"] == "match":
                    break
        db.run(
            "UPDATE listings SET art = ? WHERE item_id = ? AND kind = ?",
            json.dumps(res),
            r["item_id"],
            r["kind"],
        )


def context(db: DB) -> tuple[dict, list[dict]]:
    sold = [dict(r) for r in db.all("SELECT * FROM sold")]
    pop = [dict(r) for r in db.all("SELECT * FROM pop")]
    return value.comps(sold, dt.date.today()), pop


def verdict_for(db: DB, s: Settings, listing: dict, w: dict, cmp: dict, pop: list[dict]):
    return value.assess(
        listing,
        w,
        cmp,
        value.pop_for(pop, w),
        db.gem_rates(),
        s.grading_fee,
        s.grading_ship,
    )


def alert(db: DB, s: Settings, run_id: int, wants: dict[int, dict]) -> tuple[int, str]:
    """Alert deals not alerted before (or now 10% cheaper than when alerted). Returns (count, status)."""
    rows = [
        dict(r)
        for r in db.all(
            """SELECT l.*, a.usd AS alerted_usd FROM listings l
               LEFT JOIN alerts a ON a.item_id = l.item_id AND a.kind = l.kind
               WHERE l.active = 1 AND l.deal = 1 AND l.last_run = ?
                 AND (a.item_id IS NULL OR l.usd < a.usd * 0.9)""",
            run_id,
        )
    ]
    fresh = [
        r for r in rows if (json.loads(r["art"]) if r["art"] else {}).get("verdict") != "differs"
    ]
    if not fresh:
        return 0, "nothing new"
    cmp, pop = context(db)
    scored = []
    for r in fresh:
        w = wants[r["want_id"]]
        scored.append((r, verdict_for(db, s, r, w, cmp, pop)))
    order = {"great": 0, "good": 1, "unknown": 2, "fair": 3, "high": 4}
    scored.sort(key=lambda x: (order.get(x[1].rating, 5), x[1].total))
    results = []
    sent = 0
    for r, v in scored[: s.alert_individual]:
        art = json.loads(r["art"]) if r["art"] else {}
        tag = {"match": "art ✓", "unclear": "art ?"}.get(art.get("verdict", ""), "")
        drop = " (price drop)" if r["alerted_usd"] else ""
        title = f"{v.rating.upper()}: {v.headline}{drop}"
        body = " · ".join(
            x
            for x in [v.advice, r["time_left"] and f"auction {r['bids']} {r['time_left']}", tag]
            if x
        )
        res = notify.send(
            s.apprise_urls, title, body, s.link(f"/deal/{r['item_id']}/{r['kind']}"), "Open deal"
        )
        results.append(res)
        if res == "sent":
            sent += 1
            _mark(db, r, run_id)
    rest = scored[s.alert_individual :]
    if rest:
        lines = "\n".join(f"{v.rating}: {v.headline}" for _, v in rest[:15])
        res = notify.send(
            s.apprise_urls,
            f"Card deals: {len(rest)} more",
            lines,
            s.link(f"/run/{run_id}"),
            "Open all deals",
        )
        results.append(res)
        if res == "sent":
            sent += len(rest)
            for r, _ in rest:
                _mark(db, r, run_id)
    status = ", ".join(sorted(set(results)))
    return sent, status


def baseline(db: DB, s: Settings, run_id: int, deals: int) -> tuple[int, str]:
    """First sweep: today's deals go in the ledger unannounced, behind one summary alert."""
    for r in db.all(
        "SELECT item_id, kind, usd FROM listings WHERE last_run = ? AND deal = 1", run_id
    ):
        _mark(db, dict(r), run_id)
    res = notify.send(
        s.apprise_urls,
        f"cardhound is watching: {deals} deals live now",
        "From now on each new deal gets its own alert.",
        s.link(f"/run/{run_id}"),
        "Open deals",
    )
    return 0, f"baseline {res}"


def _mark(db: DB, r: dict, run_id: int) -> None:
    db.run(
        "INSERT INTO alerts (item_id, kind, usd, run_id) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(item_id, kind) DO UPDATE SET usd = excluded.usd, run_id = excluded.run_id, "
        "sent = datetime('now')",
        r["item_id"],
        r["kind"],
        r["usd"],
        run_id,
    )


def import_sold(db: DB, sold: dict) -> int:
    """Sales from a signed-in browser: {item_id: {set, who, kind, star, usd, date, title}}."""
    ids = {(w.get("set"), w["who"]): w["_id"] for w in db.wants()}
    rows = []
    for item_id, x in sold.items():
        want_id = ids.get((x.get("set"), x.get("who")))
        if want_id and x.get("usd"):
            rows.append(
                (
                    item_id,
                    want_id,
                    x["kind"],
                    x["star"],
                    float(x["usd"]),
                    x["date"],
                    x["title"][:200],
                )
            )
    with db.tx() as c:
        before = c.total_changes
        c.executemany(
            "INSERT OR IGNORE INTO sold (item_id, want_id, kind, star, usd, date, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return c.total_changes - before


def run(
    db: DB,
    s: Settings,
    trigger: str = "schedule",
    only: str = "",
    notify_on: bool = True,
    browser: Callable[[], Browser] | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Run one sweep; returns the run id. Refuses (returns 0) while another sweep is running."""
    if not LOCK.acquire(blocking=False):  # pylint: disable=consider-using-with
        return 0
    run_id = db.run("INSERT INTO runs (trigger) VALUES (?)", trigger)
    say = progress or (lambda m: log.info("%s", m))
    try:
        wants = db.wants(enabled_only=True)
        if only:
            subs = [o.strip().lower() for o in only.split(",") if o.strip()]
            wants = [w for w in wants if any(o in w["who"].lower() for o in subs)]
        by_id = {w["_id"]: w for w in wants}
        defaults, blocked = db.defaults(), db.blocked()
        today = dt.date.today().isoformat()
        found, sold, searched, failed = [], [], [], []
        n = 0
        factory = browser or (lambda: Browser(headless=s.headless, tries=s.fetch_tries))
        with factory() as b:
            for i, w in enumerate(wants, 1):
                f, so, ok, bad = collect(b, w, blocked, defaults, today, s.ebay_sold)
                n += len(ok) + len(bad)
                found += f
                sold += so
                searched += [(w["_id"], k) for k in ok]
                failed += [f"{w['who']} ({k})" for k in bad]
                say(f"[{i}/{len(wants)}] {w['who']}: {len(f)} listings, {len(so)} sold")
            uniq: dict[tuple[str, str], dict] = {}
            for f in found:
                uniq.setdefault((f["item_id"], f["kind"]), f)
            store(db, run_id, list(uniq.values()), sold, searched)
            say("photo check")
            photo_check(db, s, b, by_id, run_id)
        row = db.one("SELECT count(*) AS n FROM listings WHERE last_run = ? AND deal = 1", run_id)
        deals = row["n"] if row else 0
        first = not db.one("SELECT 1 FROM runs WHERE status = 'success' AND id != ?", run_id)
        if notify_on and first and not only:
            sent, status = baseline(db, s, run_id, deals)
        elif notify_on:
            sent, status = alert(db, s, run_id, by_id)
        else:
            sent, status = 0, "off for this run"
        all_failed = 0 < n == len(failed)
        db.run(
            "UPDATE runs SET finished = datetime('now'), status = ?, searches = ?, failed = ?, "
            "listings = ?, deals = ?, new_deals = ?, alerts = ? WHERE id = ?",
            "failed" if all_failed else "success",
            n,
            json.dumps(failed),
            len(uniq),
            deals,
            sent,
            status,
            run_id,
        )
        say(f"done: {len(uniq)} listings, {deals} deals, {sent} alerted ({status})")
    except Exception as e:
        log.exception("sweep failed")
        db.run(
            "UPDATE runs SET finished = datetime('now'), status = 'error', error = ? WHERE id = ?",
            f"{type(e).__name__}: {e}"[:500],
            run_id,
        )
    finally:
        LOCK.release()
    return run_id
