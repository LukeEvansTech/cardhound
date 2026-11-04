"""Which eBay listings count for a want, and what it would pay for them.

A want is a dict (stored as JSON in the wants table). Keys, all optional except set and who:

    set       short tag for the game or set: FMA, EVA, CSM, AOT (Union Arena), DBS, FIN, TLA, ...
    who       display name; for Union Arena wants also the words a title must contain
    match     words that must all appear in the title, overriding who
    q / rawq  free-form eBay query for the PSA search / the raw search (anything not Union Arena)
    id        regex the title must match (card number or name)
    need      another regex the title must match
    max       PSA ceilings in USD by star bucket: {"1": 80, "2": 800, "3": ..., "any": ...}
    rawmax    raw ceilings by star bucket; raw_max a flat raw ceiling
    raw       false to skip the raw search
    nostar    raw listings with no star marker still count
    needstar  PSA listings must carry a star marker
    refs      official-art filenames for the photo check
    cards     free-text card numbers ("011, 012 (1★, 2★)") used to find Union Arena refs
    pop       {"term": ..., "row": regex} picks this card's PSA pop rows
"""

from __future__ import annotations

import re
import urllib.parse

FOREIGN = re.compile(
    r"japan|\bjp\b|\bjpn\b|japanese|chinese|korean|\bfrench\b|german|italian|ua\d\dbt|uapr|"
    r"ua37|ua44|ex\d\dbt|\bsao\b|proxy|custom|orica|fan ?art|playmat|sleeve",
    re.I,
)
FOREIGN_SELLER = re.compile(
    r"(Located in|from) (Japan|Hong Kong|China|Korea|Taiwan|Germany|France|Italy|"
    r"Spain|Belgium|Netherlands|Australia|United Kingdom)"
)
GRADED = re.compile(r"\b(psa|bgs|cgc|tag|ace|beckett|sgc|ars)\b", re.I)

# Union Arena sets the character wants cover: eBay OR-words for the PSA search, the set name for raw
# searches, and the title hint a listing must carry to count.
UA_SETS = {
    "FMA": {
        "words": '(fma,fullmetal,alchemist,ue09bt,"union arena")',
        "raw": "fullmetal alchemist",
        "hint": r"union arena|ue09|uepr|fma-1",
    },
    "EVA": {
        "words": '(evangelion,ue15bt,"union arena")',
        "raw": "evangelion",
        "hint": r"union arena|ue15|uepr|eva-1",
    },
    "CSM": {
        "words": '("chainsaw man",csm,ue22bt,"union arena")',
        "raw": "chainsaw man",
        "hint": r"union arena|ue22|uepr|csm-1",
    },
    "AOT": {
        "words": '("attack on titan",aot,ue10bt,uex07bt,"union arena")',
        "raw": "attack on titan",
        "hint": r"union arena|ue10|uex07|uepr|aot-[12]",
    },
}
SET_HINT = {k: re.compile(v["hint"], re.I) for k, v in UA_SETS.items()}
# A Union Arena print in the card list of a want, e.g. UE22BT/CSM-1-056_p1 (set, card id, parallel).
PRINT_ID = re.compile(r"\b([A-Z0-9]+)/([A-Z]+-(?:\d-)?(?:\d{3}|AP\d{2})(?:_p\d)?)")
# The card number a seller writes in a title, e.g. EVA-1-083 or CSM-1-AP03.
TITLE_NO = re.compile(r"\b[A-Z]{2,4}-\d-(\d{3}|AP\d{2})\b", re.I)


def card_numbers(cards: str) -> set[str]:
    """Card numbers a want lists, from print ids and bare numbers ("081, 082", "UEPR 010/012")."""
    out = {i.split("-")[-1].split("_")[0].upper() for _, i in PRINT_ID.findall(cards or "")}
    rest = PRINT_ID.sub(" ", cards or "")
    card_id = re.compile(r"\b[A-Z]{2,4}-\d-(\d{3}|AP\d{2})(?:_p\d)?(?![0-9])", re.I)
    out |= {n.upper() for n in card_id.findall(rest)}  # EVA-1-081(_p1) without its set prefix
    rest = card_id.sub(" ", rest)
    out |= {n.upper() for n in re.findall(r"(?<![\w-])(AP\d{2})(?!\d)", rest, re.I)}
    out |= set(re.findall(r"(?<![\d-])(\d{3})(?!\d)", rest))
    return out


US_ZIP = "97035"  # the Oregon forwarding address: eBay prices shipping and eligibility for it


def stars(title: str) -> str:
    """Star bucket from a listing title: 3 (serial), 2 (signature), 1 (alt art), - (none)."""
    t = re.sub(r"(\d)\s*-\s*star", r"\1 star", title.lower())
    t = re.sub(r"\b([123])\s*[★☆]", r"\1 star", t)
    if re.search(r"3 ?stars?|★★★|☆☆☆|sr\*\*\*|serial", t):
        return "3"
    if re.search(r"2 ?stars?|★★|☆☆|sr\*\*|signature", t):
        return "2"
    if re.search(r"1 ?star|alt(ernate)? ?art|parallel|★|☆|\*", t):
        return "1"
    return "-"


def searches(w: dict) -> list[tuple[str, str]]:
    """(kind, query) pairs for one want; kind is psa or raw."""
    if "q" in w:
        out = [("psa", w["q"])]
        if w.get("rawq") and w.get("raw", True):
            out.append(("raw", w["rawq"]))
        return out
    if w.get("set") not in UA_SETS:
        return []
    name = w.get("match") or w["who"]
    ua = UA_SETS[w["set"]]
    out = [("psa", f"{name} psa 10 {ua['words']}")]
    if w.get("raw", True):
        out.append(("raw", f"union arena {ua['raw']} {name} -psa -bgs -cgc -tag"))
    return out


def search_url(q: str, sold: bool = False) -> str:
    p = {"_nkw": q, "_ipg": "240", "_sop": "15", "_stpos": US_ZIP}
    if sold:
        p |= {"LH_Sold": "1", "LH_Complete": "1", "_sop": "13"}
    return "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode(p)


def keep(w: dict, kind: str, title: str, text: str) -> bool:
    """Does this listing (title plus the search card's full text) belong to the want?"""
    t = title.lower()
    # A free-form query (q) drops the name check, but explicit match words still apply.
    words = (w.get("match") or ("" if "q" in w else w["who"])).lower().split()
    if any(x not in t for x in words):
        return False
    for key in ("id", "need"):
        if w.get(key) and not re.search(w[key], t, re.I):
            return False
    if w.get("set") in SET_HINT and not SET_HINT[w["set"]].search(t):
        return False
    if w.get("set") in UA_SETS and w.get("cards"):
        # A title naming a card number the want doesn't list is another card (083, not 081/082).
        seen = {n.upper() for n in TITLE_NO.findall(title)}
        listed = card_numbers(w["cards"])
        if seen and listed and not seen & listed:  # no readable numbers: no check
            return False
    if FOREIGN.search(t) or FOREIGN_SELLER.search(text):
        return False
    if w.get("set") == "DBS" and re.search(r"signed|autograph|\bauto\b|sabat|schemmel|nadolny", t):
        return False  # voice-actor autographs, not the printed Masters signature SPR
    if kind == "psa":
        if w.get("needstar") and stars(title) == "-":
            return False
        return bool(re.search(r"psa ?10|psa gem", t))
    return not GRADED.search(t) and (bool(w.get("nostar")) or stars(title) != "-")


def money(s: str) -> float:
    m = re.search(r"[\d,]+\.?\d*", s or "")
    return float(m.group(0).replace(",", "")) if m else 0.0


def shipping(text: str) -> float | None:
    """Delivery cost from a search card's text; 0 for free delivery, None when not shown."""
    if re.search(r"free (delivery|shipping)", text, re.I):
        return 0.0
    m = re.search(r"\+\s*\$([\d,]+\.?\d*)\s*(delivery|shipping)", text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def ceiling(w: dict, kind: str, star: str, defaults: dict) -> float | None:
    mx = w.get("max", {})
    if kind == "raw":
        rawmax = w.get("rawmax", {})
        if star in rawmax:
            return float(rawmax[star])
        if "any" in rawmax:
            return float(rawmax["any"])
        if "raw_max" in w:
            return float(w["raw_max"])
        return float(defaults["raw_1star_max"]) if star == "1" else None
    if star in mx:
        return float(mx[star])
    if "any" in mx:
        return float(mx["any"])
    return float(defaults["psa_1star_max"]) if star == "1" else None
