"""Web UI and API. Every alert links to /deal/<item>/<kind>; a digest links to /run/<id>."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import threading
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import match, notify, photos, sweep, value
from .config import Settings
from .db import DB

HERE = Path(__file__).resolve().parent
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
RATING_ORDER = {"great": 0, "good": 1, "unknown": 2, "fair": 3, "high": 4}


def money(v) -> str:
    return "" if v is None else f"${v:,.2f}" if v < 100 else f"${v:,.0f}"


def star_label(star: str) -> str:
    return f"{star}★" if star and star != "-" else ""


def ebay_img(src: str, size: int = 500) -> str:
    return re.sub(r"s-l\d+\.", f"s-l{size}.", src or "")


def buy_links(w: dict) -> list[tuple[str, str]]:
    """Other places to look for this card. eBay first (US view), then the shops Luke uses."""
    psa_q = match.searches(w)[0][1] if match.searches(w) else w["who"]
    plain = re.sub(r"\(.*?\)|-\S+|\bpsa 10\b", " ", psa_q)
    plain = re.sub(r"\s+", " ", plain).strip() or w["who"]
    enc = urllib.parse.quote_plus
    return [
        ("eBay: live PSA 10s", match.search_url(psa_q)),
        ("eBay: sold PSA 10s", match.search_url(psa_q, sold=True)),
        ("TCGplayer (raw)", f"https://www.tcgplayer.com/search/all/product?q={enc(plain)}"),
        (
            "GameStop (PSA slabs)",
            f"https://www.gamestop.com/search/?q={enc(plain + ' psa 10')}",
        ),
        ("Mercari US", f"https://www.mercari.com/search/?keyword={enc(plain)}"),
        ("PWCC / Fanatics Collect", f"https://www.fanaticscollect.com/search?q={enc(plain)}"),
    ]


def create_app(s: Settings, db: DB | None = None, start_scheduler: bool = False) -> FastAPI:
    db = db or DB(s.db_path)
    app = FastAPI(title="cardhound", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        """Refuse a browser write that another site's page sent (no login here, so no CSRF token)."""
        if request.method in ("POST", "PUT", "DELETE"):
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            host = urllib.parse.urlsplit(origin).netloc
            if origin and host != request.headers.get("host", ""):
                return PlainTextResponse("cross-site request refused", status_code=403)
        return await call_next(request)

    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.filters["money"] = money
    tpl.env.filters["star"] = star_label
    tpl.env.filters["ebay_img"] = ebay_img
    tpl.env.globals["version"] = s.version
    progress: list[str] = []

    def art_choices(w: dict) -> list[dict]:
        skip = set_of(w.get("skip"))
        return [
            {"name": r.name, "label": photos.label(r.name), "kept": r.name not in skip}
            for r in photos.refs_for(w, s.refs_dir)
        ]

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        return tpl.TemplateResponse(request, name, ctx)

    def start_sweep(trigger: str, want_ids: list[int] | None = None) -> bool:
        if sweep.LOCK.locked():
            return False
        progress.clear()

        def say(m: str) -> None:
            progress.append(f"{dt.datetime.now():%H:%M:%S} {m}")
            del progress[:-300]

        threading.Thread(
            target=sweep.run,
            kwargs={"db": db, "s": s, "trigger": trigger, "want_ids": want_ids, "progress": say},
            daemon=True,
        ).start()
        return True

    if start_scheduler:
        from . import scheduler

        scheduler.start(s.schedule, s.timezone, lambda: start_sweep("schedule"))

    def scored(rows: list[dict]) -> list[dict]:
        wants = {w["_id"]: w for w in db.wants()}
        cmp, pop = sweep.context(db)
        out = []
        for r in rows:
            w = wants.get(r["want_id"])
            if not w:
                continue
            r = dict(r)
            r["want"] = w
            r["v"] = sweep.verdict_for(db, s, r, w, cmp, pop)
            r["artd"] = json.loads(r["art"]) if r.get("art") else {}
            out.append(r)
        return out

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": s.version}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        last = db.one(
            "SELECT strftime('%s', finished) AS t FROM runs WHERE status = 'success' AND searches > 0 "
            "AND alerts NOT LIKE 'failed%' AND alerts NOT LIKE '%failed:%' ORDER BY id DESC LIMIT 1"
        )
        live = db.one("SELECT count(*) AS n FROM listings WHERE active = 1 AND deal = 1")
        wants = db.one("SELECT count(*) AS n FROM wants WHERE enabled = 1")
        return (
            "# HELP cardhound_last_success_timestamp_seconds End of the last sweep that searched "
            "and alerted without a failure.\n# TYPE cardhound_last_success_timestamp_seconds gauge\n"
            f"cardhound_last_success_timestamp_seconds {int(last['t']) if last and last['t'] else 0}\n"
            "# TYPE cardhound_run_in_progress gauge\n"
            f"cardhound_run_in_progress {int(sweep.LOCK.locked())}\n"
            "# TYPE cardhound_live_deals gauge\n"
            f"cardhound_live_deals {live['n'] if live else 0}\n"
            "# TYPE cardhound_wants gauge\n"
            f"cardhound_wants {wants['n'] if wants else 0}\n"
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, set: str = "", kind: str = "", show: str = "deals"):
        sql = "SELECT * FROM listings WHERE active = 1"
        args: list = []
        if show == "deals":
            sql += " AND deal = 1"
        if kind in ("psa", "raw"):
            sql += " AND kind = ?"
            args.append(kind)
        rows = scored([dict(r) for r in db.all(sql, *args)])
        if set:
            rows = [r for r in rows if r["want"].get("set") == set]
        rows.sort(key=lambda r: (RATING_ORDER.get(r["v"].rating, 9), r["v"].total))
        sets = sorted({w.get("set", "") for w in db.wants()})
        last = db.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        return page(
            request,
            "home.html",
            rows=rows[:400],
            total=len(rows),
            sets=sets,
            cur_set=set,
            kind=kind,
            show=show,
            last=last,
            running=sweep.LOCK.locked(),
        )

    @app.get("/deal/{item_id}/{kind}", response_class=HTMLResponse)
    def deal(request: Request, item_id: str, kind: str):
        row = db.one("SELECT * FROM listings WHERE item_id = ? AND kind = ?", item_id, kind)
        if not row:
            raise HTTPException(404, "listing not found")
        found = scored([dict(row)])
        if not found:
            raise HTTPException(404, "its want was deleted")
        r = found[0]
        w = r["want"]
        cmp, pop = sweep.context(db)
        card_comps = sorted(
            [(k[1], k[2], c) for k, c in cmp.items() if k[0] == w["_id"]],
            key=lambda x: (x[0], x[1]),
        )
        recent = db.all(
            "SELECT * FROM sold WHERE want_id = ? ORDER BY date DESC LIMIT 12", w["_id"]
        )
        others = scored(
            [
                dict(x)
                for x in db.all(
                    "SELECT * FROM listings WHERE want_id = ? AND active = 1 AND NOT "
                    "(item_id = ? AND kind = ?) ORDER BY usd LIMIT 8",
                    w["_id"],
                    item_id,
                    kind,
                )
            ]
        )
        refs = [p.name for p in photos.refs_for(w, s.refs_dir)]
        return page(
            request,
            "deal.html",
            r=r,
            w=w,
            card_comps=card_comps,
            recent=recent,
            others=others,
            pop=value.pop_for(pop, w)[:10],
            refs=refs,
            links=buy_links(w),
            alerted=db.one("SELECT * FROM alerts WHERE item_id = ? AND kind = ?", item_id, kind),
        )

    @app.post("/deal/{item_id}/{kind}/block")
    def block(item_id: str, kind: str, reason: str = Form("not wanted")):
        db.run(
            "INSERT OR REPLACE INTO blocked (item_id, reason) VALUES (?, ?)", item_id, reason[:200]
        )
        db.run("UPDATE listings SET active = 0 WHERE item_id = ?", item_id)
        return RedirectResponse(f"/deal/{item_id}/{kind}", status_code=303)

    @app.get("/run/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int):
        run = db.one("SELECT * FROM runs WHERE id = ?", run_id)
        if not run:
            raise HTTPException(404, "run not found")
        rows = scored(
            [
                dict(r)
                for r in db.all(
                    "SELECT l.* FROM listings l JOIN run_alerts a ON a.item_id = l.item_id "
                    "AND a.kind = l.kind WHERE a.run_id = ?",
                    run_id,
                )
            ]
        )
        if not rows:
            rows = scored(
                [
                    dict(r)
                    for r in db.all(
                        "SELECT * FROM listings WHERE last_run = ? AND deal = 1", run_id
                    )
                ]
            )
        rows.sort(key=lambda r: (RATING_ORDER.get(r["v"].rating, 9), r["v"].total))
        return page(
            request,
            "run.html",
            run=run,
            rows=rows,
            failed=json.loads(run["failed"] or "[]"),
        )

    # --- wants ---
    @app.get("/wants", response_class=HTMLResponse)
    def wants_page(request: Request, set: str = "", view: str = "cards"):
        wants = db.wants()
        counts = {
            (r["want_id"]): (r["n"], r["d"])
            for r in db.all(
                "SELECT want_id, count(*) AS n, sum(deal) AS d FROM listings WHERE active = 1 "
                "GROUP BY want_id"
            )
        }
        if set:
            wants = [w for w in wants if w.get("set") == set]
        sets = sorted({w.get("set", "") for w in db.wants()})
        thumbs = {}
        for w in wants:
            refs = photos.refs_for(w, s.refs_dir)
            kept = [r for r in refs if r.name not in set_of(w.get("skip"))]
            if refs:
                thumbs[w["_id"]] = {
                    "name": (kept or refs)[0].name,
                    "kept": len(kept),
                    "total": len(refs),
                }
        return page(
            request,
            "wants.html",
            wants=wants,
            counts=counts,
            sets=sets,
            cur_set=set,
            view="table" if view == "table" else "cards",
            thumbs=thumbs,
            defaults=db.defaults(),
        )

    @app.post("/wants/defaults")
    async def wants_defaults(request: Request):
        form = await request.form()
        cur = db.defaults()
        for key in ("psa_1star_max", "raw_1star_max"):
            raw = str(form.get(key, "")).strip().lstrip("$").replace(",", "")
            try:
                val = float(raw)
            except ValueError as e:
                raise HTTPException(400, f"{key} must be a number") from e
            if not math.isfinite(val) or val < 0:
                raise HTTPException(400, f"{key} must be a number of zero or more")
            cur[key] = val
        db.put("defaults", cur)
        return RedirectResponse("/wants", status_code=303)

    @app.get("/wants/new", response_class=HTMLResponse)
    def want_new(request: Request):
        return page(
            request, "want_form.html", w={"set": "", "who": ""}, extra="{}", error="", art=[]
        )

    @app.get("/wants/{want_id}", response_class=HTMLResponse)
    def want_edit(request: Request, want_id: int):
        w = db.want(want_id)
        if not w:
            raise HTTPException(404, "want not found")
        extra = {k: v for k, v in w.items() if k not in FORM_KEYS and not k.startswith("_")}
        return page(
            request,
            "want_form.html",
            w=w,
            extra=json.dumps(extra, indent=1, ensure_ascii=False),
            error="",
            art=art_choices(w),
        )

    @app.post("/wants")
    async def want_save(request: Request):
        data = await request.form()
        form: dict = dict(data)
        form["keep"] = [str(x) for x in data.getlist("keep")]
        form["art_all"] = [str(x) for x in data.getlist("art_all")]
        want_id = int(str(form["_id"])) if form.get("_id") else None
        old = db.want(want_id) if want_id is not None else None
        form["prev_skip"] = sorted(set_of((old or {}).get("skip")))
        try:
            spec = spec_from_form(form)
        except ValueError as e:
            echo = form_echo(form, want_id)
            return page(
                request,
                "want_form.html",
                w=echo,
                extra=form.get("extra", "{}"),
                error=str(e),
                art=art_choices(echo),
            )
        # An unticked checkbox is simply absent from the form.
        new_id = db.save_want(spec, want_id, enabled=form.get("enabled") == "on")
        # Its refs may have changed: photo verdicts are judged again on the next sweep.
        db.run("UPDATE listings SET art = NULL WHERE want_id = ?", new_id)
        saved = db.want(new_id)
        if saved:
            sweep.retire_unsearched(db, saved)
        return RedirectResponse(f"/wants/{new_id}", status_code=303)

    @app.post("/wants/{want_id}/delete")
    def want_delete(want_id: int):
        with db.tx() as c:
            c.execute(
                "DELETE FROM alerts WHERE item_id IN (SELECT item_id FROM listings WHERE want_id = ?)",
                (want_id,),
            )
            c.execute("DELETE FROM listings WHERE want_id = ?", (want_id,))
            c.execute("DELETE FROM sold WHERE want_id = ?", (want_id,))
            c.execute("DELETE FROM wants WHERE id = ?", (want_id,))
        return RedirectResponse("/wants", status_code=303)

    @app.post("/wants/{want_id}/sweep")
    def want_sweep(want_id: int):
        w = db.want(want_id)
        if not w:
            raise HTTPException(404, "want not found")
        start_sweep("manual", want_ids=[want_id])
        return RedirectResponse("/ops", status_code=303)

    # --- ops ---
    @app.get("/ops", response_class=HTMLResponse)
    def ops(request: Request):
        runs = db.all("SELECT * FROM runs ORDER BY id DESC LIMIT 50")
        from . import scheduler

        return page(
            request,
            "ops.html",
            runs=runs,
            running=sweep.LOCK.locked(),
            log=list(progress),
            schedule=s.schedule,
            next_at=scheduler.next_time(s.schedule, s.timezone),
            channels=len([u for u in s.apprise_urls.split(",") if u.strip()]),
            public_url=s.public_url,
            defaults=db.defaults(),
            gem_rates=db.gem_rates(),
        )

    @app.get("/ops/log", response_class=PlainTextResponse)
    def ops_log() -> str:
        return "\n".join(progress)

    @app.post("/ops/run")
    def ops_run():
        if not start_sweep("manual"):
            raise HTTPException(409, "a sweep is already running")
        return RedirectResponse("/ops", status_code=303)

    @app.post("/ops/test-alert")
    def ops_test():
        res = notify.send(
            s.apprise_urls,
            "cardhound test alert",
            "Tap to open the deal list.",
            s.link("/"),
            "Open cardhound",
        )
        return PlainTextResponse(res)

    # --- import, images ---
    @app.post("/api/import")
    async def api_import(request: Request) -> dict:
        """Load a wantwatch export: {wants, defaults, block, gem_rates, pop: {fetched, rows}, sold}."""
        return sweep.load_export(db, await request.json())

    @app.post("/api/sold")
    async def api_sold(request: Request) -> dict:
        body = await request.json()
        return {"added": sweep.import_sold(db, body.get("sold", {}))}

    @app.put("/api/refs/{name}")
    async def api_ref(name: str, request: Request) -> dict:
        if not SAFE_NAME.match(name) or not name.lower().endswith((".jpg", ".jpeg", ".png")):
            raise HTTPException(400, "bad file name")
        data = await request.body()
        if not 1000 < len(data) < 15_000_000:
            raise HTTPException(400, "image size out of range")
        (s.refs_dir / name).write_bytes(data)
        db.run("UPDATE listings SET art = NULL WHERE art IS NOT NULL")  # judge again, matches too
        return {"stored": name, "bytes": len(data)}

    @app.get("/api/refs")
    def api_refs() -> list[str]:
        return sorted(p.name for p in s.refs_dir.iterdir())

    @app.get("/refs/{name}")
    def ref_file(name: str):
        return _file(s.refs_dir, name)

    @app.get("/photos/{name}")
    def photo_file(name: str):
        return _file(s.photos_dir, name)

    return app


def _file(base: Path, name: str) -> FileResponse:
    if not SAFE_NAME.match(name) or not (base / name).is_file():
        raise HTTPException(404, "no such image")
    return FileResponse(base / name, headers={"Cache-Control": "max-age=86400"})


def form_echo(form: dict, want_id: int | None) -> dict:
    """The submitted form in the shape the template reads, so a validation error loses nothing."""
    w: dict = {k: str(v) for k, v in form.items() if isinstance(v, str) and not k.startswith("max")}
    for key, prefix in (("max", "max_"), ("rawmax", "rawmax_")):
        w[key] = {b: str(form[prefix + b]) for b in ("1", "2", "3", "any") if form.get(prefix + b)}
    w["refs"] = [r.strip() for r in str(form.get("refs", "")).split(",") if r.strip()]
    w["_enabled"] = form.get("enabled") == "on"
    keep = set(form.get("keep") or [])
    w["skip"] = [a for a in form.get("art_all") or [] if a not in keep]
    if want_id is not None:
        w["_id"] = want_id
    return w


FORM_KEYS = {
    "set",
    "who",
    "q",
    "rawq",
    "need",
    "id",
    "max",
    "rawmax",
    "refs",
    "cards",
    "notes",
    "skip",
}


def set_of(v) -> set[str]:
    return {str(x) for x in v} if isinstance(v, list) else set()


def spec_from_form(form: dict) -> dict:
    """Build a want from the form; the Advanced JSON box carries every other key."""
    try:
        spec = json.loads(form.get("extra") or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Advanced JSON is not valid JSON: {e}") from e
    if not isinstance(spec, dict):
        raise ValueError("Advanced JSON must be an object")
    for key in ("set", "who", "q", "rawq", "need", "id", "cards", "notes"):
        v = str(form.get(key, "")).strip()
        if v:
            spec[key] = v
        else:
            spec.pop(key, None)
    if not spec.get("who") or not spec.get("set"):
        raise ValueError("Card name and set are required")
    for key in ("need", "id"):
        if spec.get(key):
            try:
                re.compile(spec[key])
            except re.error as e:
                raise ValueError(f"'{key}' is not a valid regular expression: {e}") from e
    if isinstance(spec.get("pop"), dict) and spec["pop"].get("row"):
        try:
            re.compile(spec["pop"]["row"])
        except re.error as e:
            raise ValueError(f"pop.row is not a valid regular expression: {e}") from e
    for key, prefix in (("max", "max_"), ("rawmax", "rawmax_")):
        vals = {}
        for b in ("1", "2", "3", "any"):
            raw = str(form.get(prefix + b, "")).strip().lstrip("$").replace(",", "")
            if raw:
                try:
                    vals[b] = float(raw)
                except ValueError as e:
                    raise ValueError(f"Ceiling '{raw}' is not a number") from e
                if not math.isfinite(vals[b]) or vals[b] < 0:
                    raise ValueError(f"Ceiling '{raw}' must be a number of zero or more")
        if vals:
            spec[key] = vals
        else:
            spec.pop(key, None)
    refs = [r.strip() for r in str(form.get("refs", "")).split(",") if r.strip()]
    if refs:
        spec["refs"] = refs
    else:
        spec.pop("refs", None)
    every, keep = form.get("art_all") or [], set(form.get("keep") or [])
    # Exclusions for art not on the form (its file is missing just now) are kept as they were.
    prev = set(form.get("prev_skip") or []) | set_of(spec.get("skip"))
    skip = [a for a in sorted(prev) if a not in every]
    skip += [a for a in every if a not in keep]
    if skip:
        spec["skip"] = skip
    else:
        spec.pop("skip", None)
    if spec.get("set") not in match.UA_SETS and not spec.get("q"):
        raise ValueError("Cards outside Union Arena need an eBay search (PSA 10 query)")
    return spec
