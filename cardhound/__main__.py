"""cardhound command line.

python -m cardhound serve                 # web UI on :8080 plus the scheduled sweeps
python -m cardhound sweep [--only Vegito] [--no-notify]
python -m cardhound import export.json    # load wants, ceilings and pop from a JSON export
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import config, sweep
from .db import DB


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cardhound")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sw = sub.add_parser("sweep")
    sw.add_argument("--only", default="", help="comma-separated substrings of want names")
    sw.add_argument("--no-notify", action="store_true")
    im = sub.add_parser("import")
    im.add_argument("path")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = config.load()
    db = DB(s.db_path)

    if a.cmd == "serve":
        import uvicorn

        from .web import create_app

        app = create_app(s, db, start_scheduler=True)
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # nosec B104
        return 0
    if a.cmd == "sweep":
        run_id = sweep.run(db, s, trigger="cli", only=a.only, notify_on=not a.no_notify)
        row = db.one("SELECT status FROM runs WHERE id = ?", run_id)
        return 0 if row and row["status"] == "success" else 1
    if a.cmd == "import":
        with open(a.path, encoding="utf-8") as f:
            print(sweep.load_export(db, json.load(f)))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
