"""Compare a listing photo with the official card art.

Scores are RANSAC-verified ORB feature matches (inliers) between the listing photo and each reference
image for the want; the best reference wins. Same art scores high even through a slab or at an angle.
A low score means the photo is not the official art: a fake, the wrong card, or a back-only photo. It
cannot judge print quality, so a match means "same art", not "authentic".
"""

from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path

from .match import PRINT_ID, card_numbers

MATCH, DIFFERS = 40, 15  # inlier thresholds: >= MATCH same art, < DIFFERS different art
SMALL_REF = 400  # px on the long side
UA_TAGS = ("FMA", "EVA", "CSM", "AOT")
UA_SITE = "https://www.unionarena-tcg.com"
PRINTS = "prints"  # refs subfolder: every official print of a wanted card number, wanted or not
SIDE = 840  # px, most a reference is scaled to when one print is weighed against another
WRONG_PRINT = 2  # another print of the number scoring this many times the wanted art is that print
CODE = re.compile(r"(?<![A-Za-z])[A-Z]{2,4}-\d-(?:\d{3}|AP\d{2})(?!\d)")  # "_FMA-1-090_p1" too

# a print with its set when written: "UE09BT/FMA-1-090_p1 SR★", "UEPR_FMA-1-039.png", "FMA-1-090_p1"
PRINT_TAIL = re.compile(
    r"(?:(?<![A-Za-z0-9])([A-Z0-9]{2,8})[/_])?(?<![A-Za-z])([A-Z]{2,4}-\d-(?:\d{3}|AP\d{2})(?:_p\d+)?)(?![\w-])"
)


def prints_in(text: str) -> set[tuple[str, str]]:
    """{(set or "", number and parallel)} written in a card list or a file name."""
    return set(PRINT_TAIL.findall(text))


def listed_in(w: dict) -> set[tuple[str, str]]:
    """Prints a want names in its card list or its art file names. A bare "FMA-1-090" (no set, no
    parallel) means the number, as in refs_for(), not its plain print."""
    named = {t for t in prints_in(w.get("cards") or "") if t[0] or "_p" in t[1]}
    return named | {
        t for x in w.get("refs") or [] for t in prints_in(Path(x).stem)
    }  # even if missing


def same_print(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """One print: same number and parallel, and the same set unless either leaves it out."""
    return a[1] == b[1] and (not a[0] or not b[0] or a[0] == b[0])


def any_same(found: set[tuple[str, str]], among: set[tuple[str, str]]) -> bool:
    return any(same_print(a, b) for a in found for b in among)


log = logging.getLogger(__name__)


def refs_for(w: dict, refs_dir: Path) -> list[Path]:
    """Official images for a want: its listed refs, else Union Arena alt or promo refs by card number."""
    if w.get("refs"):
        return [refs_dir / r for r in w["refs"] if (refs_dir / r).exists()]
    tag = w.get("set") if w.get("set") in UA_TAGS else None
    if not tag:
        return []
    cards = w.get("cards", "")
    prints = [f"{a}_{b}" for a, b in PRINT_ID.findall(cards)]
    exact = [refs_dir / f"{p}{ext}" for p in prints for ext in (".jpg", ".png")]
    out = [f for f in exact if f.exists()]  # exact prints listed: those images only
    nums = card_numbers(PRINT_ID.sub(" ", cards))  # plus any card listed by number alone
    for f in sorted(refs_dir.glob(f"*_{tag}-*")):
        m = re.search(r"-\d-(\d{3})", f.name)
        if (
            f.suffix in (".jpg", ".png")
            and m
            and m.group(1) in nums
            and ("_p" in f.name or f.name.startswith("UEPR"))
        ):
            out.append(f)
    return list(dict.fromkeys(out))


def fetch_prints(w: dict, refs_dir: Path, tried: set[str] | None = None) -> None:
    """Download the official art of every print of each card number a Union Arena want lists, once
    per number. A plain SR and its SR★ parallel often share the character drawing, so a photo of the
    plain SR can pass the art check; scoring it against the plain SR too is what catches it."""
    if w.get("set") not in UA_TAGS:
        return
    d = refs_dir / PRINTS
    for code in card_codes(w, refs_dir):
        done = d / f"{code}.done"
        if done.exists() or code in (tried if tried is not None else set()):
            continue
        if tried is not None:
            tried.add(code)  # one attempt per number per sweep, failed or not
        try:
            d.mkdir(parents=True, exist_ok=True)
            url = f"{UA_SITE}/na/cardlist/?search=true&freewords={urllib.parse.quote(code)}"
            page = _get(url).decode("utf-8", "replace")
            got, bad = 0, 0
            for no, src in re.findall(r'card_no=([^"]+)"><img[^>]*data-src="([^"?]+)', page):
                if code in no:
                    ok = _save(UA_SITE + src, d / Path(src).name)
                    got, bad = got + ok, bad + (not ok)
            if got and not bad:  # an error page, a changed layout or a bad image: try next sweep
                done.touch()
            else:
                log.warning("prints of %s incomplete: %d ok, %d bad", code, got, bad)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warning("prints of %s not fetched: %s", code, e)  # tried again next sweep


def _save(url: str, f: Path) -> bool:
    """Download an image unless a good copy is cached; only an image that decodes is kept, so a
    truncated or error download is never compared (and is fetched again)."""
    if long_side(f) > 0:
        return True
    part = f.with_suffix(f.suffix + ".part")
    part.write_bytes(_get(url))
    if long_side(part) <= 0:
        part.unlink(missing_ok=True)
        return False
    part.replace(f)
    return True


def card_codes(w: dict, refs_dir: Path) -> list[str]:
    """Full card numbers ("FMA-1-090") of a Union Arena want, from its card list and its art files,
    so a want that lists a bare "090" is covered too."""
    if w.get("set") not in UA_TAGS:
        return []
    names = " ".join(p.name for p in refs_for(w, refs_dir))
    return sorted(set(CODE.findall(w.get("cards") or "")) | set(CODE.findall(names)))


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 - fixed https host
        return r.read()


def decoys_for(w: dict, refs_dir: Path) -> list[Path]:
    """Official art of the other prints of the card numbers a want lists: the plain print and any
    parallel the want doesn't list."""
    refs = refs_for(w, refs_dir)
    names = [r.stem for r in refs] + [Path(x).stem for x in w.get("refs") or []]  # missing too
    if any(not prints_in(x) for x in names):
        return []  # art of no known print (a custom name): no way to tell which catalogue art is it
    wanted = wanted_tails(w, refs)  # listed, even before or without its art
    bare = bare_numbers(w)
    out = []
    for code in card_codes(w, refs_dir):
        for f in sorted((refs_dir / PRINTS).glob(f"*_{code}*")):
            if f.suffix in (".jpg", ".png") and not (
                any_same(prints_in(f.stem), wanted) or any_parallel(f, bare, w.get("set"))
            ):
                out.append(f)
    return out


def bare_numbers(w: dict) -> set[str]:
    """Numbers listed on their own ("090"), which mean any parallel or promo, as in refs_for(),
    unless the want names its art."""
    return set() if w.get("refs") else card_numbers(PRINT_ID.sub(" ", w.get("cards") or ""))


def any_parallel(f: Path, numbers: set[str], tag: str | None) -> bool:
    """A parallel or promo of a number listed on its own, which refs_for() takes as wanted."""
    m = re.search(rf"_{tag}-\d-(\d{{3}})", f.name) if tag else None
    return bool(m and m.group(1) in numbers and ("_p" in f.name or f.name.startswith("UEPR")))


def wanted_tails(w: dict, refs: list[Path]) -> set[tuple[str, str]]:
    """Prints a want lists or has art for, so a catalogue image of a wanted print is never taken
    for another print."""
    return listed_in(w) | {t for r in refs for t in prints_in(r.stem)}


def compared(w: dict, refs_dir: Path) -> list[str]:
    """Every card list print a verdict for this want weighs, and on which side: one arriving or
    changing sides means judge again."""
    refs = refs_for(w, refs_dir)
    declared = [f"wants:{a}/{b}" for a, b in sorted(listed_in(w))]  # covered or not yet
    declared += [f"catalogued:{catalogued(w, refs_dir)}"]
    return (
        declared
        + [f"print:{d.name}" for d in decoys_for(w, refs_dir)]
        + [f"listed:{f.name}" for f in listed_prints(w, refs_dir, refs)]
    )


def listed_prints(w: dict, refs_dir: Path, refs: list[Path]) -> list[Path]:
    """Card list art of prints the want lists but has no art of its own for (nor has unticked)."""
    readable = [r for r in refs if long_side(r) > 0]  # an unreadable art needs its catalogue copy
    named = [r.stem for r in readable] + [Path(x).stem for x in w.get("skip") or []]
    have = {t for x in named for t in prints_in(x)}
    ids = {t for t in listed_in(w) if not any_same({t}, have)}
    stems, bare = set(named), bare_numbers(w)  # never an unticked art either
    return [
        f
        for f in sorted((refs_dir / PRINTS).glob("*"))
        if f.suffix in (".jpg", ".png")
        and f.stem not in stems
        and (any_same(prints_in(f.stem), ids) or any_parallel(f, bare, w.get("set")))
    ]


def wants_print(w: dict, name: str, refs_dir: Path) -> bool:
    """Could the want take the print named by a catalogue file stem ("UE09BT_FMA-1-090_p2")?
    Never an unticked one; a want whose art has no known print leaves it to the photo check."""
    seen = prints_in(name)
    if any_same(seen, {t for x in w.get("skip") or [] for t in prints_in(Path(x).stem)}):
        return False
    refs = refs_for(w, refs_dir)
    names = [r.stem for r in refs] + [Path(x).stem for x in w.get("refs") or []]
    if any(not prints_in(x) for x in names):
        return True
    f = refs_dir / PRINTS / f"{name}.png"
    return any_same(seen, wanted_tails(w, refs)) or any_parallel(f, bare_numbers(w), w.get("set"))


def catalogued(w: dict, refs_dir: Path) -> bool:
    """Every print of each card number of the want is here (fetch_prints() finished)."""
    return all((refs_dir / PRINTS / f"{c}.done").exists() for c in card_codes(w, refs_dir))


def uncovered(w: dict, ticked: list[Path]) -> bool:
    """True while a print the want lists (and hasn't unticked) has no art to compare yet."""
    skipped = {t for x in w.get("skip") or [] for t in prints_in(Path(x).stem)}
    have = {t for r in ticked for t in prints_in(r.stem)}
    listed = {t for t in listed_in(w) if not any_same({t}, skipped)}
    return any(not any_same({t}, have) for t in listed)


def listed_rarity(w: dict) -> dict:
    """{image stem: rarity} as the card list of the want writes it ("UE22BT/CSM-1-056_p1 SR★★")."""
    out = {}
    for m in re.finditer(PRINT_ID.pattern + r"\s+([A-Z]{1,3}★*)(?=[\s,]|$)", w.get("cards", "")):
        out[f"{m.group(1)}_{m.group(2)}"] = m.group(3)
    return out


def upgrade(src: str) -> str:
    return re.sub(r"s-l\d+\.(jpg|jpeg|webp|png)", r"s-l1600.\1", src)


def download(src: str, dest: Path) -> Path | None:
    """eBay's image CDN serves plain HTTP clients, unlike its pages."""
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    if not src.startswith("https://"):
        return None
    try:
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 - https only, checked above
            dest.write_bytes(r.read())
        return dest
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def score(photo: Path, ref: Path, side: int | None = None) -> int:
    """RANSAC inliers between the photo and a reference; side scales the reference to that long
    side first, so references of different sizes compare fairly."""
    import cv2
    import numpy as np

    a = cv2.imread(str(ref), cv2.IMREAD_GRAYSCALE)
    b = cv2.imread(str(photo), cv2.IMREAD_GRAYSCALE)
    if a is None or b is None:
        return -1

    def fit(m):
        f = 1000 / max(m.shape)
        return cv2.resize(m, None, fx=f, fy=f) if f < 1 else m

    a = cv2.resize(a, None, fx=side / max(a.shape), fy=side / max(a.shape)) if side else fit(a)
    b = fit(b)
    orb = cv2.ORB_create(3000)  # type: ignore[attr-defined]
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < 10 or len(kb) < 10:
        return 0
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < 8:
        return len(good)
    src = np.array([ka[m.queryIdx].pt for m in good], dtype=np.float32).reshape((-1, 1, 2))
    dst = np.array([kb[m.trainIdx].pt for m in good], dtype=np.float32).reshape((-1, 1, 2))
    _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 6.0)
    return int(mask.sum()) if mask is not None else 0


def check(w: dict, key: str, src: str, refs_dir: Path, photos_dir: Path) -> dict:
    """{'verdict': match|differs|unclear|unchecked, 'score', 'ref', 'photo'} for one photo."""
    own = refs_for(w, refs_dir)
    refs = own + listed_prints(w, refs_dir, own)  # wanted prints it has no art of: catalogue art
    none = {"verdict": "unchecked", "score": None, "ref": None, "photo": None}
    if not refs or not src:
        return none
    photo = download(upgrade(src), photos_dir / f"{key}.jpg")
    if not photo:
        return none
    scored = [(score(photo, r), r) for r in refs]
    best, ref = max(scored, key=lambda x: x[0])
    if best < 0:  # an image that would not decode is not evidence either way
        photo.unlink(missing_ok=True)  # so the next sweep downloads it again
        return none
    verdict = "match" if best >= MATCH else "differs" if best < DIFFERS else "unclear"
    skip = set(w.get("skip") or [])
    if verdict == "differs" and any(small(r) for r in refs if r.name not in skip):
        verdict = "unclear"  # missing a thumbnail-sized wanted art proves nothing
    if verdict == "match" and ref.name in skip:
        # 1★ and 2★ of one card share their art, so only call it unwanted when no kept art matches too.
        kept = max((sc for sc, r in scored if r.name not in skip), default=0)
        if kept < MATCH:
            verdict = "unwanted"
    decoys = decoys_for(w, refs_dir)
    other = None
    ticked = [r for r in refs if r.name not in skip] + listed_prints(w, refs_dir, refs)
    ticked = [r for r in ticked if long_side(r) > 0]  # an unreadable art leaves its print uncovered
    if (
        verdict in ("match", "unclear")
        and decoys
        and catalogued(w, refs_dir)
        and not uncovered(w, ticked)
    ):
        # at one reference size: card list prints and wanted art come in different sizes
        side = min([SIDE] + [n for n in map(long_side, ticked + decoys) if n > 0])  # no upscaling
        mine = max((score(photo, r, side) for r in ticked), default=0)
        top, near = max(((score(photo, d, side), d) for d in decoys), key=lambda x: x[0])
        if top >= MATCH and top > WRONG_PRINT * mine:
            verdict, other = "unwanted", near.stem  # the photo is that print, not a wanted one
    return {
        "verdict": verdict,
        "score": best,
        "ref": ref.name,
        "photo": photo.name,
        "refs": ref_key(w, refs, compared(w, refs_dir)),
        **({"print": other} if other else {}),
    }


def ref_key(w: dict, refs: list[Path], prints: list[str] | None = None) -> list[str]:
    """What a verdict was judged against: the art, the card list prints (compared()) and the
    unticked art; a change means judge again."""
    return (
        sorted(r.name for r in refs)
        + list(prints or [])
        + [f"skip:{x}" for x in sorted(w.get("skip") or [])]
    )


def long_side(ref: Path) -> int:
    """Long side of an image in px, 0 when it does not decode."""
    import cv2

    m = cv2.imread(str(ref), cv2.IMREAD_UNCHANGED)
    return max(m.shape[:2]) if m is not None else 0


def small(ref: Path) -> bool:
    """True for a reference too small to rule a photo out (the DBS card list's 260px scans)."""
    import cv2

    m = cv2.imread(str(ref), cv2.IMREAD_UNCHANGED)
    return m is not None and max(m.shape[:2]) < SMALL_REF


def label(name: str, rarity: dict | None = None) -> str:
    """Short caption for a reference image: card number and printed rarity, or promo.

    The _p suffix only counts parallel prints (CSM-1-048_p1 is SR★★, _p2 is SR★), so the star
    tier comes from the card list's rarity when it is known, never from the suffix.
    """
    m = re.search(r"^(UEPR)?.*?-\d-(\d{3}|AP\d{2})", name)
    if m:
        promo, num = m.groups()
        known = (rarity or {}).get(Path(name).stem)
        text = f"promo {num}" if promo else num
        return f"{text} {known}" if known else text
    m = re.search(r"^MTG_([a-z0-9]+)-(\d+)", name)
    if m:
        return f"{m.group(1).upper()} #{m.group(2)}"
    return Path(name).stem
