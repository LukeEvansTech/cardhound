# cardhound

A personal trading-card deal finder. It searches eBay.com (US view, priced for delivery to a US
forwarding address) for every card on a want list, judges each listing against your ceiling, recent
sold prices and the cost of grading a raw copy, and sends one alert per new deal. Each alert opens
that deal's own page, with the listing photo beside the official art and a verdict on whether to
buy the slab, buy raw and grade, or pass.

## How it works

```text
sweep (cron) ─▶ eBay search per want (headless Chromium) ─▶ listings
              ─▶ ceiling + comps + gem rate ─▶ verdict per listing
              ─▶ photo check against the official art (OpenCV ORB)
              ─▶ alert each new deal (Apprise), deal page per alert
```

- **Wants** are edited on the Wants page (or loaded with `POST /api/import`). A Union Arena want
  (set `FMA`, `EVA`, `CSM`, `AOT`) builds its searches from the card name; any other card takes an
  eBay query. Ceilings are per star bucket, in USD, compared with the landed price (price plus
  delivery).
- **Verdict**: `great` / `good` / `fair` / `high` against the 90-day sold median for the same
  card, kind (PSA 10 or raw) and star bucket, plus the cost of one PSA 10 by grading a raw copy:
  `(raw price + grading fee + shipping) / gem rate`. The gem rate is the card's own PSA pop when 10
  or more have been graded, otherwise the set's.
- **Sold comps**: eBay sends signed-out sold searches to a sign-in page, so sold prices are posted
  to `POST /api/sold` by a signed-in browser elsewhere.
- **Alerts**: the first sweep records a baseline and sends one summary. After that each new deal
  (or a deal now 10% cheaper than when alerted) gets its own alert, up to
  `CARDHOUND_ALERT_INDIVIDUAL` per sweep; the rest go in one digest that opens the sweep's page. A
  listing whose photo does not match the official art is held back. A listing is marked alerted
  only after the send succeeds.

## Run it

```sh
uv venv -p 3.13 .venv && uv pip install -p .venv -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q
CARDHOUND_DATA=data CARDHOUND_SCHEDULE='0 0 1 1 *' .venv/bin/python -m cardhound serve   # :8080
.venv/bin/python -m cardhound sweep --only Vegito --no-notify
```

Docker: `docker build -t cardhound .` then `docker run -v cardhound:/data -p 8080:8080 cardhound`.
The image runs as uid 65532 with a read-only root filesystem; give it a writable `/tmp`.

## Configuration

| Variable                     | Default             | Description                                                        |
| ---------------------------- | ------------------- | ------------------------------------------------------------------ |
| `CARDHOUND_DATA`             | `data`              | Directory for `cardhound.db`, official art (`refs/`) and photos.   |
| `CARDHOUND_PUBLIC_URL`       | none                | Base URL of the web UI; alert links are absolute when set.         |
| `CARDHOUND_SCHEDULE`         | `0 2,8,14,20 * * *` | Cron for the in-process sweep.                                     |
| `CARDHOUND_TZ`               | `Europe/London`     | Timezone the schedule is read in.                                  |
| `APPRISE_URLS`               | none                | Comma-separated Apprise URLs. Empty sends nothing.                 |
| `CARDHOUND_ALERT_INDIVIDUAL` | `5`                 | Deals per sweep that get their own alert; the rest share a digest. |
| `CARDHOUND_GRADING_FEE`      | `39.99`             | PSA fee per card, USD.                                             |
| `CARDHOUND_GRADING_SHIP`     | `6`                 | Return shipping per card, USD.                                     |
| `CARDHOUND_TRIES`            | `8`                 | Page loads per search before it counts as failed.                  |

## API

| Route                  | Purpose                                                                                   |
| ---------------------- | ----------------------------------------------------------------------------------------- |
| `POST /api/import`     | `{wants, defaults, gem_rates, block, pop: {fetched, rows}}`; wants match on set and name. |
| `POST /api/sold`       | `{sold: {item_id: {set, who, kind, star, usd, date, title}}}`.                            |
| `PUT /api/refs/<name>` | Upload one official-art image (JPEG or PNG).                                              |
| `GET /health`          | Process is up.                                                                            |
| `GET /metrics`         | `cardhound_last_success_timestamp_seconds` and friends, for alerting.                     |

The UI has no login: run it behind an internal-only route.
