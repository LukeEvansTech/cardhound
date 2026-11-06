"""Read a graded listing's slab label from its photo, so a CGC (or other) slab sold as PSA 10 is dropped.

Titles lie: a CGC 9 was listed as "PSA 10" (2026-11-06). The photo check compares art only, so a
vision model (the cluster's LiteLLM, OpenAI-compatible) reads the label instead.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

GRADERS = {"PSA", "CGC", "BGS", "SGC", "TAG", "ACE", "ARS", "OTHER", "NONE"}

PROMPT = """This is a photo from an eBay listing of a graded trading card. Read the grading label on
the slab (the plastic case) and answer in JSON only:

{"grader": "PSA" | "CGC" | "BGS" | "SGC" | "TAG" | "ACE" | "ARS" | "OTHER" | "NONE",
 "grade": "the numeric grade on the label, e.g. 10 or 9.5, or empty",
 "sure": true | false}

PSA labels have a red border and the PSA logo; CGC labels say CGC / CERTIFIED GUARANTY COMPANY;
BGS / Beckett labels are silver or gold with subgrades. NONE: no slab or no label in the photo.
sure is true only when the label's company is clearly legible."""


def read(url: str, key: str, model: str, photo: Path, timeout: int = 90) -> dict | None:
    """{'grader', 'grade', 'sure'} from the photo, or None when the model gave no usable answer."""
    jpeg = as_jpeg(photo)
    if jpeg is None:
        return None
    data = base64.b64encode(jpeg).decode()
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 200,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{data}"},
                        },
                    ],
                }
            ],
        }
    ).encode()
    req = urllib.request.Request(
        f"{url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - configured endpoint
            text = json.loads(r.read())["choices"][0]["message"]["content"] or ""
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warning("slab read failed: %s", type(e).__name__)
        return None
    return parse(text)


def as_jpeg(photo: Path) -> bytes | None:
    """The photo as JPEG bytes: eBay may serve PNG or WebP under a .jpg name, and the model
    refuses an image whose bytes do not match its declared type."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(photo.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes() if ok else None


def parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    grader = str(v.get("grader", "")).upper() if isinstance(v, dict) else ""
    if grader not in GRADERS:
        return None
    return {
        "grader": grader,
        "grade": str(v.get("grade") or "").strip(),
        "sure": v.get("sure") is True,
    }


def wrong(v: dict | None) -> str:
    """Why a slab read rules out a PSA 10 listing, or '' when it does not."""
    if not v or not v["sure"] or v["grader"] == "NONE":
        return ""
    if v["grader"] != "PSA":
        return f"slab is {v['grader']} {v['grade']}".strip()
    if v["grade"] and v["grade"] not in ("10", "10.0"):
        return f"slab is PSA {v['grade']}"
    return ""
