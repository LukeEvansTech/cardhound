"""Alerts through Apprise. Each alert opens its own deal page, so one message never carries 40 cards."""

from __future__ import annotations

import logging
import urllib.parse

log = logging.getLogger(__name__)


def _with_link(url: str, link: str, link_title: str) -> str:
    """Pushover shows a supplementary URL as a button; other channels get the link in the body."""
    if not link or not url.startswith(("pover://", "pushover://")):
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + urllib.parse.urlencode({"url": link, "url_title": link_title})


def send(urls: str, title: str, body: str, link: str = "", link_title: str = "Open deal") -> str:
    """'sent', 'off' (no channels configured) or 'failed: <reason>'. Credentials never reach the log."""
    targets = [u.strip() for u in urls.split(",") if u.strip()]
    if not targets:
        return "off"
    try:
        import apprise

        a = apprise.Apprise()
        for u in targets:
            a.add(_with_link(u, link, link_title))
        text = f"{body}\n{link}" if link else body
        ok = a.notify(title=title[:250], body=text[:1000])
        return "sent" if ok else "failed: a channel refused the message"
    except Exception as e:  # pylint: disable=broad-exception-caught
        return f"failed: {type(e).__name__}"
