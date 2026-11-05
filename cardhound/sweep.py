"""One sweep: search eBay for every enabled want, record listings and sales, check photos, alert.

Ledger rule: a listing is marked alerted only after its alert was sent, so a failed send is retried
on the next sweep instead of being lost.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import re
import threading
from typing import Any, Callable, Protocol

from . import match, notify, photos, value
from .config import Settings
from .db import DB
from .ebay import Browser
from .ebay_api import BrowseAPI


class Source(Protocol):
    """Where searches go: the headless browser or the Browse API (same URLs in, same rows out)."""

    def __enter__(self) -> "Source": ...

    def __exit__(self, *exc: object) -> None: ...

    def search(self, url: str) -> list[dict] | None: ...

    def item_photos(self, item_id: str) -> list[str]: ...


log = logging.getLogger(__name__)
LOCK = threading.Lock()
ITEM_PAGES_PER_RUN = 25


def _sold_date(text: str, today: str) -> str:
    m = re.search(r"Sold\s+([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
    if not m:
        return today
    return dt.datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()


def collect(
    b: Source,
    w: dict,
    blocked: set[str],
    defaults: dict,
    today: str,
    sold_too: bool,
    cache: dict | None = None,
):
    """(listings, sold, ok_kinds, failed_kinds) for one want.

    ``cache`` holds this sweep's search results by URL: the prints split from one card share a query,
    so eBay is asked once and each print filters the same rows."""
    found, sold, ok, failed = [], [], [], []
    cache = {} if cache is None else cache

    def fetch(url: str) -> list[dict] | None:
        if url not in cache:
            cache[url] = b.search(url)
        return cache[url]

    for kind, q in match.searches(w):
        rows = fetch(match.search_url(q))
        if rows is None:
            failed.append(kind)
            continue
        ok.append(kind)
        for r in (fetch(match.search_url(q, sold=True)) if sold_too else None) or []:
            if r["id"] in blocked or not match.keep(w, kind, r["title"], r["text"]):
                continue
            if match.money(r["price"]) <= 0:
                continue
            sold.append(
                {
                    "item_id": r["id"],
                    "want_id": match.comp_id(w),
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
            star = match.stars(r["title"])  # never the print's own: 1★ and 2★ share their art
            usd = match.money(r["price"])
            if usd <= 0:  # price element missing or unparsed: never a $0 deal
                continue
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


def fit(f: dict, w: dict, matched: int | None) -> tuple:
    """Sort key for the print a listing belongs to, best first.

    A photo already matched to a print wins; then a title naming the print's number, then its star
    count; then the lowest ceiling, so a listing the title and photo can't place never becomes a deal
    on the strength of a sibling's higher ceiling."""
    one = match.print_of(w)
    title = f["title"]
    named = starred = False
    if one:
        num, star = one
        named = num in {n.upper() for n in match.TITLE_NO.findall(title)} or num in set(
            match.BARE_NO.findall(title)
        )
        starred = match.stars(title) == star != "-"
    cap = f["cap"] if f["cap"] is not None else -1.0
    return (w["_id"] != matched, not named, not starred, cap, w["_id"])


def assign(db: DB, found: list[dict], wants: dict[int, dict]) -> dict[tuple[str, str], dict]:
    """One want per listing: the prints split from one card can all claim the same listing."""
    cands: dict[tuple[str, str], list[dict]] = {}
    for f in found:
        cands.setdefault((f["item_id"], f["kind"]), []).append(f)
    matched = {}
    for r in db.all("SELECT item_id, kind, want_id, art FROM listings WHERE art IS NOT NULL"):
        if json.loads(r["art"]).get("verdict") == "match":
            matched[(r["item_id"], r["kind"])] = r["want_id"]
    live = {x["_id"] for x in db.wants()}
    out = {}
    for key, fs in cands.items():
        fs = [f for f in fs if f["want_id"] in wants] or fs
        m = matched.get(key)
        if m is not None and m in live and m not in {f["want_id"] for f in fs}:
            continue  # its photo placed it on a print this sweep didn't search; leave it there
        ranked = sorted(
            (fit(f, wants.get(f["want_id"], {"_id": f["want_id"]}), m), i) for i, f in enumerate(fs)
        )
        out[key] = fs[ranked[0][1]]
    return out


def store(db: DB, run_id: int, found: list[dict], sold: list[dict], searched: list[tuple]) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    wants = db.wants()
    live = {w["_id"] for w in wants}  # a want deleted mid-sweep must not come back
    blocked = db.blocked()  # nor a listing blocked mid-sweep
    found = [f for f in found if f["want_id"] in live and f["item_id"] not in blocked]
    sold = [x for x in sold if x["want_id"] in live | {match.comp_id(w) for w in wants}]
    searched = [x for x in searched if x[0] in live]
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


def retire_unsearched(db: DB, w: dict) -> None:
    """A kind the want no longer searches (raw switched off, say) has no live listings left."""
    kinds = sorted({k for k, _ in match.searches(w)})
    marks = ",".join("?" * len(kinds)) or "''"
    db.run(
        f"UPDATE listings SET active = 0 WHERE want_id = ? AND kind NOT IN ({marks})",  # nosec B608
        w["_id"],
        *kinds,
    )


def photo_check(db: DB, s: Settings, b: Source | None, wants: dict[int, dict], run_id: int) -> None:
    """Score each live deal's photo against the official art, once per listing."""
    rows = db.all(
        "SELECT item_id, kind, want_id, img, art, star, usd, ship, title FROM listings "
        "WHERE active = 1 AND deal = 1 "
        "AND last_run = ?",
        run_id,
    )
    pages = ITEM_PAGES_PER_RUN
    everyone = {x["_id"]: x for x in db.wants(enabled_only=True)}  # siblings outside this sweep too
    for r in rows:
        w = wants.get(r["want_id"])
        old = json.loads(r["art"]) if r["art"] else {}
        if not w:
            continue
        stale = old.get("verdict") == "differs" and any(  # judged before the small-ref rule
            photos.small(p)
            for p in photos.refs_for(w, s.refs_dir)
            if p.name not in (w.get("skip") or [])
        )
        refs_now = photos.ref_key(w, photos.refs_for(w, s.refs_dir))
        moved = old.get("verdict") not in (None, "unchecked") and old.get("refs") != refs_now
        if (
            old
            and old.get("verdict") != "unchecked"
            and not old.get("retry")
            and not (stale or moved)
        ):
            continue
        res = photos.check(w, r["item_id"], r["img"], s.refs_dir, s.photos_dir)
        if res["verdict"] in ("differs", "unclear", "unwanted"):
            srcs = []
            if b is not None and pages > 0:
                pages -= 1
                srcs = b.item_photos(r["item_id"])[:4]
            complete = bool(srcs)
            for i, src in enumerate(srcs):
                alt = photos.check(w, f"{r['item_id']}_{i}", src, s.refs_dir, s.photos_dir)
                complete = complete and alt["verdict"] != "unchecked"
                if alt["verdict"] == "match":  # a kept art matching beats any other score
                    res = alt
                    break
                if (alt["score"] or 0) > (res["score"] or 0):
                    res = alt
            # Until one of the listing's own photos was scored, the thumbnail verdict is provisional.
            res["retry"] = not complete and res["verdict"] != "match"
        if (
            res["verdict"] != "match"
            and res.get("photo")
            and move_to_sibling(db, s, r, w, everyone, res)
        ):
            continue
        db.run(
            "UPDATE listings SET art = ? WHERE item_id = ? AND kind = ?",
            json.dumps(res),
            r["item_id"],
            r["kind"],
        )


def move_to_sibling(
    db: DB, s: Settings, r: Any, w: dict, wants: dict[int, dict], res: dict
) -> bool:
    """A photo that is another print of the same card moves the listing to that print's want."""
    family = match.comp_id(w)
    sibs = [
        x
        for x in wants.values()
        if x["_id"] != w["_id"]
        and match.comp_id(x) == family
        and r["kind"] in {k for k, _ in match.searches(x)}  # never into a print with raw off
        and match.keep(x, r["kind"], r["title"], r["title"])  # star tier and number must agree
    ]
    if not sibs or not w.get("family") and not any(x.get("family") for x in sibs):
        return False
    key = res["photo"].rsplit(".", 1)[0]  # the photo already downloaded for the best score
    for sib in sibs:
        got = photos.check(sib, key, "file:", s.refs_dir, s.photos_dir)
        if got["verdict"] != "match":
            continue
        cap = match.ceiling(sib, r["kind"], r["star"], db.defaults())
        deal = int(cap is not None and r["usd"] + (r["ship"] or 0) <= cap)
        db.run(
            "UPDATE listings SET want_id = ?, cap = ?, deal = ?, art = ? WHERE item_id = ? AND kind = ?",
            sib["_id"],
            cap,
            deal,
            json.dumps(got),
            r["item_id"],
            r["kind"],
        )
        return True
    return False


def rate(db: DB, s: Settings, run_id: int, wants: dict[int, dict]) -> int:
    """Drop deals the sold comps say are fair or high; returns how many were dropped."""
    rows = [
        dict(r) for r in db.all("SELECT * FROM listings WHERE last_run = ? AND deal = 1", run_id)
    ]
    if not rows:
        return 0
    cmp, pop = context(db)
    drop = []
    for r in rows:
        w = wants.get(r["want_id"])
        if w and not verdict_for(db, s, r, w, cmp, pop).is_deal:
            drop.append((r["item_id"], r["kind"]))
    with db.tx() as c:
        c.executemany("UPDATE listings SET deal = 0 WHERE item_id = ? AND kind = ?", drop)
    return len(drop)


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
                 AND (a.item_id IS NULL OR l.usd + coalesce(l.ship, 0) <= a.usd * 0.9)""",
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
        w = wants.get(r["want_id"])
        if not w:
            continue
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
    """First alerting sweep: one summary alert; today's deals join the ledger only once it is sent."""
    res = notify.send(
        s.apprise_urls,
        f"cardhound is watching: {deals} deals live now",
        "From now on each new deal gets its own alert.",
        s.link(f"/run/{run_id}"),
        "Open deals",
    )
    if res == "sent":
        for r in db.all(
            "SELECT item_id, kind, usd, ship, art FROM listings WHERE last_run = ? AND deal = 1",
            run_id,
        ):
            # A provisional photo mismatch stays out of the ledger so a later match can alert.
            if (json.loads(r["art"]) if r["art"] else {}).get("verdict") != "differs":
                _mark(db, dict(r), run_id)
        db.put("baseline_done", True)
    return 0, f"baseline {res}"


def _mark(db: DB, r: dict, run_id: int) -> None:
    db.run(
        "INSERT INTO alerts (item_id, kind, usd, run_id) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(item_id, kind) DO UPDATE SET usd = excluded.usd, run_id = excluded.run_id, "
        "sent = datetime('now')",
        r["item_id"],
        r["kind"],
        r["usd"] + (r.get("ship") or 0),  # landed price, as deals are judged
        run_id,
    )
    db.run(
        "INSERT OR IGNORE INTO run_alerts (run_id, item_id, kind) VALUES (?, ?, ?)",
        run_id,
        r["item_id"],
        r["kind"],
    )


def load_export(db: DB, body: dict) -> dict:
    """Load {wants, defaults, gem_rates, ref_labels, block, pop, sold}; wants match on set and name."""
    added = updated = 0
    existing = {(w.get("set"), w["who"]): w["_id"] for w in db.wants()}
    for w in body.get("wants", []):
        key = (w.get("set"), w["who"])
        want_id = db.save_want(w, existing.get(key))
        db.run("UPDATE listings SET art = NULL WHERE want_id = ?", want_id)
        if key in existing:
            updated += 1
        else:
            added += 1
    if isinstance(body.get("ref_labels"), dict):  # {image stem: printed rarity, e.g. "SR★★"}
        db.put("ref_labels", {**db.get("ref_labels", {}), **body["ref_labels"]})
    for setting in ("defaults", "gem_rates"):
        if setting in body:
            db.put(setting, body[setting])
    for item, why in (body.get("block") or {}).items():
        db.run("INSERT OR REPLACE INTO blocked (item_id, reason) VALUES (?, ?)", item, why)
    pop = body.get("pop") or {}
    if pop.get("rows"):
        with db.tx() as c:
            c.execute("DELETE FROM pop")
            c.executemany(
                "INSERT INTO pop (set_name, term, descr, gems, total, fetched) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        p["set"],
                        p.get("term"),
                        p["desc"],
                        p["gems"],
                        p["total"],
                        pop.get("fetched", ""),
                    )
                    for p in pop["rows"]
                ],
            )
    sold = import_sold(db, body["sold"]) if body.get("sold") else 0
    return {"added": added, "updated": updated, "pop": len(pop.get("rows", [])), "sold": sold}


def import_sold(db: DB, sold: dict) -> int:
    """Sales from a signed-in browser: {item_id: {set, who, kind, star, usd, date, title}}."""
    ids = {}
    for w in db.wants():  # a split print is pushed under its card's name; comps go to the family
        ids[(w.get("set"), w["who"])] = match.comp_id(w)
        if w.get("family") and w.get("match"):
            ids.setdefault((w.get("set"), w["match"]), match.comp_id(w))
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


def default_source(s: Settings) -> Source:
    """The Browse API when a keyset is configured, otherwise headless Chromium."""
    if s.ebay_api:
        return BrowseAPI(s.ebay_client_id, s.ebay_client_secret)
    return Browser(headless=s.headless, tries=s.fetch_tries)


def run(
    db: DB,
    s: Settings,
    trigger: str = "schedule",
    only: str = "",
    notify_on: bool = True,
    want_ids: list[int] | None = None,
    browser: Callable[[], Source] | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Run one sweep; returns the run id. Refuses (returns 0) while another sweep is running."""
    if not LOCK.acquire(blocking=False):  # pylint: disable=consider-using-with
        return 0
    # The thread lock covers the server; the file lock also stops a CLI sweep overlapping it.
    lock_path = s.data_dir / "sweep.lock"
    try:
        lock_file = open(lock_path, "w", encoding="utf-8")  # pylint: disable=consider-using-with
    except OSError:
        LOCK.release()
        raise
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        LOCK.release()
        return 0
    run_id = 0
    say = progress or (lambda m: log.info("%s", m))
    try:
        run_id = db.run("INSERT INTO runs (trigger) VALUES (?)", trigger)
        wants = db.wants(enabled_only=True)
        if only:
            subs = [o.strip().lower() for o in only.split(",") if o.strip()]
            wants = [w for w in wants if any(o in w["who"].lower() for o in subs)]
        if want_ids:
            wants = [w for w in wants if w["_id"] in want_ids]
        by_id = {w["_id"]: w for w in wants}
        defaults, blocked = db.defaults(), db.blocked()
        today = dt.date.today().isoformat()
        found, sold, searched, failed = [], [], [], []
        n = 0
        say(f"source: {'eBay Browse API' if s.ebay_api and not browser else 'browser'}")
        cache: dict = {}
        with (browser or (lambda: default_source(s)))() as b:
            for i, w in enumerate(wants, 1):
                f, so, ok, bad = collect(b, w, blocked, defaults, today, s.ebay_sold, cache)
                n += len(ok) + len(bad)
                found += f
                sold += so
                searched += [(w["_id"], k) for k in ok]
                failed += [f"{w['who']} ({k})" for k in bad]
                say(f"[{i}/{len(wants)}] {w['who']}: {len(f)} listings, {len(so)} sold")
            uniq = assign(db, found, by_id)
            store(db, run_id, list(uniq.values()), sold, searched)
            for w in wants:
                retire_unsearched(db, w)
            demoted = rate(db, s, run_id, by_id)
            say(f"price check: {demoted} under the ceiling but not below recent sales")
            say("photo check")
            photo_check(db, s, b, by_id, run_id)
            rate(db, s, run_id, by_id)  # a listing moved to a sibling print is rated as that print
            db.run(  # a photo of art Luke has unticked on the want is not a deal either
                "UPDATE listings SET deal = 0 WHERE last_run = ? AND deal = 1 "
                "AND json_extract(art, '$.verdict') = 'unwanted'",
                run_id,
            )
        row = db.one("SELECT count(*) AS n FROM listings WHERE last_run = ? AND deal = 1", run_id)
        deals = row["n"] if row else 0
        first = not db.get("baseline_done", False)
        if not searched:
            sent, status = 0, "nothing searched"
        elif notify_on and first and not only and not want_ids:
            sent, status = baseline(db, s, run_id, deals)
        elif notify_on:
            sent, status = alert(db, s, run_id, by_id)
        else:
            sent, status = 0, "off for this run"
        all_failed = n == len(failed)  # also true when nothing was searched at all
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
        if not run_id:
            raise
        db.run(
            "UPDATE runs SET finished = datetime('now'), status = 'error', error = ? WHERE id = ?",
            f"{type(e).__name__}: {e}"[:500],
            run_id,
        )
    finally:
        lock_file.close()
        LOCK.release()
    return run_id
