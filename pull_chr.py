"""Chart-access (CHR) events.  DISABLED unless CHR_ENABLED=true.

ASSUMPTION: chart-open events are available as a Metabase card (same server and
API key as the main pull). Add a `chr_events` block with its own `card_id` to
config/column_map.yaml (a commented example is already there). No new secret needed.
"""
from __future__ import annotations

import argparse
from datetime import date

import requests

from .common import (env_flag, get_conn, load_column_map, load_settings,
                     replace_rows, require_env)
from .pull_metabase import daterange, fetch_card_day, select_table


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pull chart-access events (only if CHR_ENABLED=true).")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--peek", action="store_true", help="print the first rows (mapped columns only) and do not save")
    a = ap.parse_args(argv)
    if not env_flag("CHR_ENABLED"):
        raise SystemExit("CHR is disabled. Set CHR_ENABLED=true in .env to use it.")
    settings = load_settings()
    spec = load_column_map().get("chr_events")
    if not spec or not spec.get("card_id"):
        raise SystemExit("Add a 'chr_events' block (with card_id and columns) to config/column_map.yaml first.")
    env_vals = require_env("METABASE_URL", "METABASE_API_KEY")
    session = requests.Session()
    session.headers.update({"X-API-KEY": env_vals["METABASE_API_KEY"]})
    conn = get_conn(settings=settings)
    total = 0
    for day in daterange(date.fromisoformat(a.start), date.fromisoformat(a.end)):
        raw = fetch_card_day(session, env_vals["METABASE_URL"], str(spec["card_id"]), day, settings)
        if raw.empty:
            continue
        sub, missing = select_table(raw, spec["columns"])
        if missing:
            print(f"WARNING: columns not found: {missing}")
        if a.peek:
            print(sub.head(5).to_string())
            return
        sub["_pull_day"] = day.isoformat()
        replace_rows(conn, "raw_chr_events", sub, "_pull_day", day.isoformat())
        total += len(sub)
    print(f"Stored {total} CHR events.")


if __name__ == "__main__":
    main()
