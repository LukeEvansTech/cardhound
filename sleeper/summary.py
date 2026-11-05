"""The daily summary: deals alerted since the last summary, auctions about to end, sweep health.

One gather() feeds both the /summary page and the email. The email embeds each card photo as an
inline attachment so it shows in clients that block remote images (Outlook does by default).
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import re
import smtplib
import ssl
import threading
import time
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import sweep
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

RATING_ORDER = {"great": 0, "good": 1, "unknown": 2, "fair": 3, "high": 4}
LAST_RUN = "summary_last_run"  # kv: the newest run the last sent summary covered
FIRST_FROM = "summary_first_from"  # kv: where the first summary starts, until one is sent
LAST_HTML = "summary_last_html"  # kv: the last sent email, with web photos
MAX_PHOTOS = 60  # download attempts per email
LOCK = threading.Lock()  # one gather-send-save at a time
MAX_CARDS = 40  # per email; the rest are a link to the Deals page


def gather(db: DB, s: Settings, now: dt.datetime | None = None) -> dict:
    """What a summary would say right now; ``last_run`` is where the next one starts."""
    now = now or dt.datetime.now()
    # only finished runs: a running sweep may not have recorded its alerts yet
    newest = db.one("SELECT max(id) AS id FROM runs WHERE status != 'running'")
    last_run = int(newest["id"] or 0) if newest else 0
    after = db.get(LAST_RUN, None)
    if after is None:  # first summary: the day before the first look, kept until one is sent
        after = db.get(FIRST_FROM, None)
        if after is None:
            day_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            first = db.one(
                "SELECT min(id) AS id FROM runs WHERE coalesce(finished, started) >= ?", day_ago
            )
            after = (first["id"] - 1) if first and first["id"] else last_run
            db.put(FIRST_FROM, after)
    after = int(after)

    wants = {w["_id"]: w for w in db.wants()}
    cmp, pop = sweep.context(db)
    live: list[dict] = []
    for row in db.all("SELECT * FROM listings WHERE active = 1 AND deal = 1"):
        w = wants.get(row["want_id"])
        if not w:
            continue
        r = dict(row)
        if (json.loads(r["art"]) if r["art"] else {}).get("verdict") == "differs":
            continue  # the photo shows another print: alerts withhold these too
        if r["auction"] and r.get("ends") and r["ends"] < now.isoformat(timespec="minutes"):
            continue  # ended since the last sweep saw it
        r["want"] = w
        r["v"] = sweep.verdict_for(db, s, r, w, cmp, pop)
        live.append(r)
    alerted = {
        (a["item_id"], a["kind"])
        for a in db.all(
            "SELECT item_id, kind FROM run_alerts WHERE run_id > ? AND run_id <= ?", after, last_run
        )
    }

    def best(r: dict) -> tuple:
        return (RATING_ORDER.get(r["v"].rating, 9), r["v"].total)

    new = sorted((r for r in live if (r["item_id"], r["kind"]) in alerted), key=best)
    soon = (now + dt.timedelta(days=1)).isoformat(timespec="minutes")
    ending = sorted(
        (
            r
            for r in live
            if r["auction"] and r.get("ends") and now.isoformat() <= r["ends"] <= soon
        ),
        key=lambda r: r["ends"],
    )
    runs = db.all(
        "SELECT status, searches, failed, alerts FROM runs WHERE id > ? AND id <= ?",
        after,
        last_run,
    )
    # a broken sweep or alert is a problem; a few failed searches are noise, retried next sweep
    bad = sum(1 for r in runs if r["status"] != "success" or "failed" in r["alerts"])
    return {
        "now": now,
        "new_graded": [r for r in new if r["kind"] == "psa"],
        "new_raw": [r for r in new if r["kind"] == "raw"],
        "new_count": len(new),
        "ending": ending,
        "live": len(live),
        "live_graded": sum(1 for r in live if r["kind"] == "psa"),
        "live_raw": sum(1 for r in live if r["kind"] == "raw"),
        "runs": len(runs),
        "bad_runs": bad,
        "searches": sum(r["searches"] for r in runs),
        "failed_searches": sum(len(json.loads(r["failed"] or "[]")) for r in runs),
        "last_run": last_run,
    }


def subject(g: dict) -> str:
    if not g["new_count"]:
        return f"Sleeper: no new deals, {g['live']} still live"
    parts = [
        f"{len(g[k])} {label}"
        for k, label in (("new_graded", "graded"), ("new_raw", "raw"))
        if g[k]
    ]
    n = g["new_count"]
    return f"Sleeper: {n} new deal{'s' if n != 1 else ''} ({', '.join(parts)})"


def _thumb(url: str) -> bytes | None:
    """A small JPEG of the listing photo, or None (the card shows without one)."""
    if not url.startswith("https://i.ebayimg.com/"):
        return None
    small = re.sub(r"s-l\d+\.", "s-l225.", url)
    try:
        with urllib.request.urlopen(small, timeout=15) as resp:  # nosec B310: https eBay CDN only
            data = resp.read(400_000)
        return data if data[:3] == b"\xff\xd8\xff" else None
    except OSError:
        return None


def send(s: Settings, g: dict, render) -> str:
    """Send the email; 'sent' or 'failed: <reason>'. ``render(g, img)`` returns (html, text)."""
    cids: dict[str, tuple[str, bytes]] = {}
    shown = g["ending"][:MAX_CARDS] + g["new_graded"][:MAX_CARDS] + g["new_raw"][:MAX_CARDS]
    tried: set[str] = set()
    misses = 0
    for r in shown:
        if r["img"] in tried or len(tried) >= MAX_PHOTOS or misses >= 3:
            continue  # three misses in a row: the CDN is down, send without photos
        tried.add(r["img"])
        data = _thumb(r["img"])
        if data:
            cids[r["img"]] = (make_msgid(domain="sleeper")[1:-1], data)
            misses = 0
        else:
            misses += 1
    html, text = render(g, lambda r: f"cid:{cids[r['img']][0]}" if r["img"] in cids else "")
    msg = EmailMessage()
    msg["Subject"] = subject(g)
    msg["From"] = formataddr(("Sleeper", s.mail_from))
    msg["To"] = s.summary_to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    body = msg.get_body(("html",))
    for cid, data in cids.values():
        if isinstance(body, EmailMessage):
            body.add_related(data, "image", "jpeg", cid=f"<{cid}>", disposition="inline")
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=60) as smtp:
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            refused = smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        log.warning("summary email failed: %s", type(e).__name__)
        return f"failed: {type(e).__name__}"
    if refused:  # some recipients accepted, some not: retry them all next time
        return f"failed: {len(refused)} recipient(s) refused"
    return "sent"


def web_img(r: dict) -> str:
    """The listing photo from eBay's CDN, for the web copy of an email."""
    return re.sub(r"s-l\d+\.", "s-l225.", r["img"] or "")


def _settled(db: DB, s: Settings, wait: float = 3600) -> dict | None:
    """gather() once no sweep is running, in this server or a CLI one; None after ``wait``."""
    deadline = time.monotonic() + wait
    if not sweep.LOCK.acquire(timeout=wait):  # pylint: disable=consider-using-with
        return None
    try:
        with open(s.data_dir / "sweep.lock", "w", encoding="utf-8") as f:
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        return None
                    time.sleep(5)
            try:
                return gather(db, s)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        sweep.LOCK.release()


def run(db: DB, s: Settings, render) -> str:
    """Gather once sweeps settle, send, and move the start point on only when the email went."""
    with LOCK:
        g = _settled(db, s)
        if g is None:
            res = "failed: a sweep ran for over an hour"
        else:
            res = send(s, g, render)
            if res == "sent":
                db.put(LAST_RUN, g["last_run"])
                db.put(LAST_HTML, render(g, web_img)[0])  # the copy "This email on the web" opens
        db.put("summary_last_result", f"{dt.datetime.now().isoformat(timespec='minutes')} {res}")
    log.info("summary email: %s", res)
    return res
