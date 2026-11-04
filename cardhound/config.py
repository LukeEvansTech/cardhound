"""Settings from the environment. Every value has a default that works on a laptop."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    return float(raw) if raw.strip() else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("CARDHOUND_DATA", "data")))
    public_url: str = field(
        default_factory=lambda: os.environ.get("CARDHOUND_PUBLIC_URL", "").rstrip("/")
    )
    schedule: str = field(
        default_factory=lambda: os.environ.get("CARDHOUND_SCHEDULE", "0 2,8,14,20 * * *")
    )
    timezone: str = field(default_factory=lambda: os.environ.get("CARDHOUND_TZ", "Europe/London"))
    apprise_urls: str = field(default_factory=lambda: os.environ.get("APPRISE_URLS", ""))
    # Alerts per run sent one by one (each opens its own deal page); the rest go in one digest.
    alert_individual: int = field(
        default_factory=lambda: int(os.environ.get("CARDHOUND_ALERT_INDIVIDUAL", "5"))
    )
    grading_fee: float = field(default_factory=lambda: _float("CARDHOUND_GRADING_FEE", 39.99))
    grading_ship: float = field(default_factory=lambda: _float("CARDHOUND_GRADING_SHIP", 6.0))
    fetch_tries: int = field(default_factory=lambda: int(os.environ.get("CARDHOUND_TRIES", "8")))
    # eBay sends signed-out sold searches to sign-in and a captcha, so sold comps arrive through
    # POST /api/sold from a signed-in browser instead. Set CARDHOUND_EBAY_SOLD=1 to try anyway.
    ebay_sold: bool = field(
        default_factory=lambda: os.environ.get("CARDHOUND_EBAY_SOLD", "0") == "1"
    )
    headless: bool = field(default_factory=lambda: os.environ.get("CARDHOUND_HEADLESS", "1") != "0")
    version: str = field(default_factory=lambda: os.environ.get("APP_VERSION", "0.0.0-dev"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cardhound.db"

    @property
    def refs_dir(self) -> Path:
        return self.data_dir / "refs"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    def link(self, path: str) -> str:
        return f"{self.public_url}{path}" if self.public_url else path


def load() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.refs_dir, s.photos_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
