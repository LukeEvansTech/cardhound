"""Is a listing good value, and is it cheaper to buy a raw copy and grade it?

Everything here is pure: it takes the listing, the want, sold comps and PSA pop, and returns the
verdict the deal page and the alert show.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
from dataclasses import dataclass, field

UA_POP_TAG = {"FMA": "fma", "EVA": "eva", "CSM": "csm", "AOT": "aot"}


@dataclass
class Comp:
    median: float
    count: int
    last: str
    low: float
    high: float


def comps(sold: list[dict], today: dt.date, days: int = 90) -> dict[tuple[int, str, str], Comp]:
    """(want_id, kind, star) -> sold-price summary over the last `days` days."""
    cut = (today - dt.timedelta(days=days)).isoformat()
    by: dict[tuple[int, str, str], list[tuple[float, str]]] = {}
    for x in sold:
        if x["date"] >= cut and x["usd"] > 0:
            by.setdefault((x["want_id"], x["kind"], x["star"]), []).append((x["usd"], x["date"]))
    return {
        k: Comp(
            median=statistics.median(p for p, _ in v),
            count=len(v),
            last=max(d for _, d in v),
            low=min(p for p, _ in v),
            high=max(p for p, _ in v),
        )
        for k, v in by.items()
    }


def star_of(desc: str) -> str:
    m = re.search(r"(\d) star", desc.lower())
    return m.group(1) if m else "-"


def pop_for(pop: list[dict], w: dict) -> list[dict]:
    """PSA pop rows for one want: its pop term and row regex, else every name word in its set."""
    if w.get("pop"):
        term, row = w["pop"].get("term"), w["pop"].get("row", "")
        hits = [p for p in pop if p.get("term") == term and re.search(row, p["descr"], re.I)]
        return sorted(hits, key=lambda p: -p["total"])
    tag = UA_POP_TAG.get(w.get("set", ""))
    if not tag:
        return []
    words = (w.get("match") or w["who"]).lower().split()
    hits = []
    for p in pop:
        d, s = p["descr"].lower(), p["set_name"].lower()
        if ("mari makinami" in d and "mari" not in words) or (tag not in s and "promo" not in s):
            continue
        if all(re.search(rf"(?<![a-z0-9]){re.escape(x)}(?![a-z0-9])", d) for x in words):
            hits.append(p)
    return sorted(hits, key=lambda p: -p["total"])


def gem_rate(w: dict, star: str, pop_rows: list[dict], set_rates: dict) -> tuple[float | None, str]:
    """Chance a raw copy grades PSA 10: the card's own pop when 10+ have been graded, else the set's."""
    rows = [p for p in pop_rows if star_of(p["descr"]) == star]
    gems, total = sum(p["gems"] for p in rows), sum(p["total"] for p in rows)
    if total >= 10:
        return gems / total, f"this card's PSA pop ({gems}/{total})"
    rate = set_rates.get(w.get("set", ""))
    if rate:
        return float(rate), f"the {w['set']} set average"
    return None, ""


@dataclass
class Verdict:
    rating: str  # great | good | fair | high | unknown
    headline: str
    advice: str
    reasons: list[str] = field(default_factory=list)
    total: float = 0.0
    grade_cost: float | None = None
    max_bid: float | None = None
    buy_to_grade: bool = False

    @property
    def is_deal(self) -> bool:
        """Under the ceiling is not enough: the price must also beat recent sales, or grading must pay."""
        return self.rating in ("great", "good", "unknown") or self.buy_to_grade


def _pct(a: float, b: float) -> str:
    return f"{a / b:.0%}"


def assess(
    listing: dict,
    w: dict,
    cmp: dict,
    pop_rows: list[dict],
    set_rates: dict,
    fee: float,
    grade_ship: float,
) -> Verdict:
    kind, star, usd = listing["kind"], listing["star"], float(listing["usd"])
    ship = listing.get("ship")
    total = usd + (ship or 0)
    cap = listing.get("cap")
    wid = w["_id"]
    ten, raw = cmp.get((wid, "psa", star)), cmp.get((wid, "raw", star))
    rate, rate_src = gem_rate(w, star, pop_rows, set_rates)
    reasons: list[str] = []
    ship_txt = (
        "free delivery" if ship == 0 else f"${ship:,.2f} delivery" if ship else "delivery not shown"
    )
    reasons.append(f"${usd:,.2f} + {ship_txt} = ${total:,.2f} landed at the Oregon address")

    if cap is not None:
        if total <= cap:
            reasons.append(f"Under your ceiling of ${cap:,.0f} by ${cap - total:,.0f}")
        else:
            reasons.append(f"Over your ceiling of ${cap:,.0f} by ${total - cap:,.0f}")

    same = ten if kind == "psa" else raw
    label = "PSA 10" if kind == "psa" else "raw"
    ratio = None
    if same:
        ratio = total / same.median
        reasons.append(
            f"{_pct(total, same.median)} of the {label} sold median ${same.median:,.0f} "
            f"({same.count} sale{'s' * (same.count != 1)} in 90 days, ${same.low:,.0f}-${same.high:,.0f}, "
            f"last {same.last})"
        )
    else:
        reasons.append(f"No {label} sales of this card recorded in the last 90 days")

    grade_cost = None
    buy_to_grade = False
    if rate:
        base, what = (total, "this copy") if kind == "raw" else (None, "")
        if kind != "raw" and raw:
            base, what = raw.median, f"a raw copy at the ${raw.median:,.0f} median"
        if base is not None:
            grade_cost = (base + fee + grade_ship) / rate
            reasons.append(
                f"Grading {what}: ${base:,.0f} + ${fee:.2f} PSA Value + ${grade_ship:.0f} shipping, "
                f"÷ {rate:.0%} gem rate ({rate_src}) = ${grade_cost:,.0f} per PSA 10"
            )

    if ratio is None:
        rating = "unknown" if cap is None or total > cap else "good"
    elif ratio <= 0.75:
        rating = "great"
    elif ratio <= 0.92:
        rating = "good"
    elif ratio <= 1.08:
        rating = "fair"
    else:
        rating = "high"

    if kind == "psa":
        if grade_cost and grade_cost < total * 0.8:
            advice = (
                f"Buying raw and grading is cheaper (~${grade_cost:,.0f} per 10 against ${total:,.0f}), "
                "but takes weeks and the gem rate is a gamble."
            )
        elif rating in ("great", "good") and ten:
            advice = "Buy the slab: the price is below what PSA 10s have been selling for."
        elif rating == "good":
            advice = "Under your ceiling, but there are no PSA 10 sales to compare it with."
        elif rating == "fair":
            advice = "Fair market price. Buy if you want it now; otherwise offer below the median."
        elif rating == "high":
            advice = "Pass or offer: priced above recent PSA 10 sales."
        else:
            advice = "No sales history yet: judge it on your ceiling alone."
    else:
        if grade_cost and ten:
            if grade_cost < ten.median * 0.85:
                buy_to_grade = True
                advice = f"Buy to grade: ~${grade_cost:,.0f} per PSA 10 against a ${ten.median:,.0f} slab median."
            else:
                advice = (
                    f"Grading this costs ~${grade_cost:,.0f} per 10, close to the ${ten.median:,.0f} "
                    "slab median: buy the slab instead unless you want the raw."
                )
        elif rating in ("great", "good") and ten:
            advice = (
                f"Good raw price. No gem rate for {w.get('set', 'this set')} yet, so the cost of "
                f"grading it cannot be set against the ${ten.median:,.0f} slab median."
            )
        elif rating in ("great", "good"):
            advice = "Good raw price. No PSA 10 sales yet to compare a graded copy against."
        else:
            advice = "Judge on the photos and your ceiling; there is little sales history."

    max_bid = None
    if listing.get("auction"):
        anchors = [x for x in (cap, ten.median * 0.95 if kind == "psa" and ten else None) if x]
        if kind == "raw" and ten and rate:
            anchors.append(ten.median * 0.85 * rate - fee - grade_ship)
        if anchors:
            max_bid = max(0.0, min(anchors) - (ship or 0))
            advice += f" Auction: a snipe up to ${max_bid:,.0f} keeps it a deal."

    star_txt = f"{star}★ " if star != "-" else ""
    headline = f"{w['who']} {star_txt}{'PSA 10' if kind == 'psa' else 'raw'} ${total:,.0f}"
    return Verdict(rating, headline, advice, reasons, total, grade_cost, max_bid, buy_to_grade)
