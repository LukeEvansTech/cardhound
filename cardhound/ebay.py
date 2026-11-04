"""eBay.com search and listing pages through headless Chromium.

Plain HTTP gets a 403 from eBay. A headless browser gets through, but eBay still fails a share of page
loads at random (an "Error Page" with status 403), so every load retries with a pause.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
EXTRACT = """() => [...document.querySelectorAll('li.s-card')].map(c => ({
  id: c.getAttribute('data-listingid') || '',
  title: ((c.querySelector('.s-card__title') || {}).innerText || '')
    .replace(/New listing|Opens in a new window or tab/g, '').trim(),
  price: (c.querySelector('.s-card__price') || {}).innerText || '',
  text: c.innerText.replace(/\\s+/g, ' '),
  img: (i => i ? (i.getAttribute('src') || i.getAttribute('data-src') || '') : '')(c.querySelector('img'))
})).filter(r => r.id && r.title && r.title !== 'Shop on eBay')"""
ITEM_PHOTOS = """() => [...new Set([...document.querySelectorAll('img')]
  .map(i => i.src || i.getAttribute('data-src') || '')
  .filter(s => /i\\.ebayimg\\.com\\/images\\/g\\//.test(s)).map(s => s.replace(/s-l\\d+/, 's-l1600')))]"""
NO_RESULTS = "text=/No exact matches found|0 results/"


class Browser:
    """One headless Chromium page reused for a whole sweep."""

    def __init__(self, headless: bool = True, tries: int = 8, pause: float = 6.0):
        self.headless, self.tries, self.pause = headless, tries, pause
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self.page: Any = None

    def __enter__(self) -> "Browser":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # The full Chromium build in new-headless mode: the slimmer headless shell crashes its
        # renderer on eBay's 240-result pages inside a container.
        self._browser = self._pw.chromium.launch(
            channel="chromium", headless=self.headless, args=["--disable-dev-shm-usage"]
        )
        self._ctx = self._browser.new_context(
            locale="en-US", viewport={"width": 1366, "height": 900}, user_agent=UA
        )
        self.page = self._ctx.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _wait(self) -> None:
        time.sleep(self.pause * random.uniform(0.7, 1.4))
        if self.page.is_closed():  # a crashed renderer closes the page; carry on with a new one
            self.page = self._ctx.new_page()

    def search(self, url: str) -> list[dict] | None:
        """Result cards for a search URL; [] for a genuine no-results page, None if every try failed."""
        for attempt in range(self.tries):
            try:
                resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                if resp is not None and resp.status == 200:
                    if self.page.locator("li.s-card").count():
                        return list(self.page.evaluate(EXTRACT))
                    if self.page.locator(NO_RESULTS).count():
                        return []
                    self.page.wait_for_selector("li.s-card", timeout=15000)
                    return list(self.page.evaluate(EXTRACT))
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.debug("search attempt %d failed: %s", attempt, type(e).__name__)
            self._wait()
        return None

    def item_photos(self, item_id: str) -> list[str]:
        """Full-size photo URLs from a listing page, gallery order."""
        for _ in range(self.tries):
            try:
                resp = self.page.goto(
                    f"https://www.ebay.com/itm/{item_id}",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                if resp is not None and resp.status == 200:
                    self.page.wait_for_selector("h1", timeout=15000)
                    return list(self.page.evaluate(ITEM_PHOTOS))
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.debug("item page failed: %s", type(e).__name__)
            self._wait()
        return []
