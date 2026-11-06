"""Place a snipe on Gixen (gixen.com), which bids in an eBay auction's last seconds.

Gixen's API is one HTTPS GET: api.php with the account login, itemid and maxbid. It answers
"OK <item> ADDED" or "ERROR (<code>): <text>"; code 111 means the account has no Gixen Plus
subscription, which the API requires. The login travels in the query string, as Gixen's API
defines it, so the URL is never logged.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request

API = "https://www.gixen.com/api.php"


class GixenError(Exception):
    pass


def snipe(user: str, password: str, item_id: str, maxbid: float, timeout: int = 30) -> str:
    """Add a snipe; returns Gixen's answer ("OK ... ADDED"), or raises GixenError with its error."""
    if not (user and password):
        raise GixenError("no Gixen login configured")
    if not re.fullmatch(r"\d{1,20}", item_id):
        raise GixenError(f"not an eBay item number: {item_id!r}")
    if not maxbid or maxbid <= 0:
        raise GixenError("the maximum bid must be above zero")
    query = urllib.parse.urlencode(
        {
            "username": user,
            "password": password,
            "notags": 1,
            "itemid": item_id,
            "maxbid": f"{maxbid:.2f}",
        }
    )
    req = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": "sleeper"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - fixed https URL
            body = r.read(4000).decode("utf-8", "replace")
    except Exception as e:  # pylint: disable=broad-exception-caught
        raise GixenError(f"Gixen did not answer ({type(e).__name__})") from None
    text = re.sub(r"<[^>]+>", " ", body).strip()
    if re.search(rf"\bOK {item_id} ADDED\b", text):
        return f"OK {item_id} ADDED"
    m = re.search(r"ERROR \((\d+)\):\s*([^\n<]*)", text)
    raise GixenError(f"Gixen error {m.group(1)}: {m.group(2).strip()}" if m else text[:200])
