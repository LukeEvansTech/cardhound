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
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("SLEEPER_DATA", "data")))
    public_url: str = field(
        default_factory=lambda: os.environ.get("SLEEPER_PUBLIC_URL", "").rstrip("/")
    )
    schedule: str = field(
        default_factory=lambda: os.environ.get("SLEEPER_SCHEDULE", "0 2,8,14,20 * * *")
    )
    timezone: str = field(default_factory=lambda: os.environ.get("SLEEPER_TZ", "Europe/London"))
    apprise_urls: str = field(default_factory=lambda: os.environ.get("APPRISE_URLS", ""))
    # Alerts per run sent one by one (each opens its own deal page); the rest go in one digest.
    alert_individual: int = field(
        default_factory=lambda: int(os.environ.get("SLEEPER_ALERT_INDIVIDUAL", "5"))
    )
    grading_fee: float = field(default_factory=lambda: _float("SLEEPER_GRADING_FEE", 39.99))
    grading_ship: float = field(default_factory=lambda: _float("SLEEPER_GRADING_SHIP", 6.0))
    fetch_tries: int = field(default_factory=lambda: int(os.environ.get("SLEEPER_TRIES", "8")))
    # eBay sends signed-out sold searches to sign-in and a captcha, so sold comps arrive through
    # POST /api/sold from a signed-in browser instead. Set SLEEPER_EBAY_SOLD=1 to try anyway.
    ebay_sold: bool = field(default_factory=lambda: os.environ.get("SLEEPER_EBAY_SOLD", "0") == "1")
    # Browse API keyset (production, client credentials). When both are set the sweep searches
    # through the API instead of headless Chromium.
    ebay_client_id: str = field(default_factory=lambda: os.environ.get("EBAY_CLIENT_ID", ""))
    ebay_client_secret: str = field(
        default_factory=lambda: os.environ.get("EBAY_CLIENT_SECRET", "")
    )
    # Daily summary email through an SMTP relay; off until host, sender and recipient are all set.
    smtp_host: str = field(default_factory=lambda: os.environ.get("SLEEPER_SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SLEEPER_SMTP_PORT", "25")))
    mail_from: str = field(default_factory=lambda: os.environ.get("SLEEPER_MAIL_FROM", ""))
    summary_to: str = field(default_factory=lambda: os.environ.get("SLEEPER_SUMMARY_TO", ""))
    summary_schedule: str = field(
        default_factory=lambda: os.environ.get("SLEEPER_SUMMARY_SCHEDULE", "30 8 * * *")
    )
    headless: bool = field(default_factory=lambda: os.environ.get("SLEEPER_HEADLESS", "1") != "0")
    version: str = field(default_factory=lambda: os.environ.get("APP_VERSION", "0.0.0-dev"))

    @property
    def ebay_api(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)

    @property
    def summary_on(self) -> bool:
        return bool(self.smtp_host and self.mail_from and self.summary_to)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sleeper.db"

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
    old = s.data_dir / "cardhound.db"  # the app was called cardhound until 2026-11-05
    if old.exists() and not s.db_path.exists():
        # sidecars first, the database last: an interrupted move resumes on the next start
        for suffix in ("-wal", "-shm", ""):
            src = old.with_name(old.name + suffix)
            if src.exists():
                src.rename(s.db_path.with_name(s.db_path.name + suffix))
    return s
