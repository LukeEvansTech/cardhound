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

MATCH, DIFFERS = 40, 15  # inlier thresholds: >= MATCH same art, < DIFFERS different art
UA_TAGS = ("FMA", "EVA", "CSM", "AOT")


def refs_for(w: dict, refs_dir: Path) -> list[Path]:
    """Official images for a want: its listed refs, else Union Arena alt or promo refs by card number."""
    if w.get("refs"):
        return [refs_dir / r for r in w["refs"] if (refs_dir / r).exists()]
    tag = w.get("set") if w.get("set") in UA_TAGS else None
    if not tag:
        return []
    nums = set(re.findall(r"\b(\d{3})\b", w.get("cards", "")))
    out = []
    for f in sorted(refs_dir.glob(f"*_{tag}-*.jpg")):
        m = re.search(r"-\d-(\d{3})", f.name)
        if m and m.group(1) in nums and ("_p" in f.name or f.name.startswith("UEPR")):
            out.append(f)
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
    best, ref = max(((score(photo, r), r) for r in refs), key=lambda x: x[0])
    if best < 0:  # an image that would not decode is not evidence either way
        photo.unlink(missing_ok=True)  # so the next sweep downloads it again
        return none
    verdict = "match" if best >= MATCH else "differs" if best < DIFFERS else "unclear"
    return {"verdict": verdict, "score": best, "ref": ref.name, "photo": photo.name}
