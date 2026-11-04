"""Compare a listing photo with the official card art.

Scores are RANSAC-verified ORB feature matches (inliers) between the listing photo and each reference
image for the want; the best reference wins. Same art scores high even through a slab or at an angle.
A low score means the photo is not the official art: a fake, the wrong card, or a back-only photo. It
cannot judge print quality, so a match means "same art", not "authentic".
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

from .match import PRINT_ID, card_numbers

MATCH, DIFFERS = 40, 15  # inlier thresholds: >= MATCH same art, < DIFFERS different art
SMALL_REF = 400  # px on the long side
UA_TAGS = ("FMA", "EVA", "CSM", "AOT")


def refs_for(w: dict, refs_dir: Path) -> list[Path]:
    """Official images for a want: its listed refs, else Union Arena alt or promo refs by card number."""
    if w.get("refs"):
        return [refs_dir / r for r in w["refs"] if (refs_dir / r).exists()]
    tag = w.get("set") if w.get("set") in UA_TAGS else None
    if not tag:
        return []
    cards = w.get("cards", "")
    prints = [f"{a}_{b}" for a, b in PRINT_ID.findall(cards)]
    if prints:  # exact prints listed: those and nothing else
        found = [refs_dir / f"{p}{ext}" for p in prints for ext in (".jpg", ".png")]
        return [f for f in found if f.exists()]
    nums = card_numbers(cards)
    out = []
    for f in sorted(refs_dir.glob(f"*_{tag}-*")):
        m = re.search(r"-\d-(\d{3})", f.name)
        if (
            f.suffix in (".jpg", ".png")
            and m
            and m.group(1) in nums
            and ("_p" in f.name or f.name.startswith("UEPR"))
        ):
            out.append(f)
    return out


def listed_rarity(w: dict) -> dict:
    """{image stem: rarity} as the want's card list writes it ("UE22BT/CSM-1-056_p1 SR★★")."""
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


def score(photo: Path, ref: Path) -> int:
    import cv2
    import numpy as np

    a = cv2.imread(str(ref), cv2.IMREAD_GRAYSCALE)
    b = cv2.imread(str(photo), cv2.IMREAD_GRAYSCALE)
    if a is None or b is None:
        return -1

    def fit(m):
        f = 1000 / max(m.shape)
        return cv2.resize(m, None, fx=f, fy=f) if f < 1 else m

    a, b = fit(a), fit(b)
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
    refs = refs_for(w, refs_dir)
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
    return {"verdict": verdict, "score": best, "ref": ref.name, "photo": photo.name}


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
