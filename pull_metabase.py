"""Pull the master Metabase report day by day into DuckDB (raw_* tables).

Usage:  python -m src.pull_metabase --start 2026-03-01 --end 2026-03-07

Only columns listed in config/column_map.yaml are kept; everything else is
dropped immediately, so unexpected columns (e.g. names) never reach disk.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta

import pandas as pd
import requests

from .common import (get_conn, load_column_map, load_settings, replace_rows,
                     require_env)

SKIP_TABLES = {"chr_events"}  # handled by pull_chr.py


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def build_parameters(day: date, settings: dict) -> list[dict]:
    mb = settings.get("metabase", {})
    ptype = mb.get("param_type", "date/single")
    iso = day.isoformat()
    return [
        {"type": ptype, "target": ["variable", ["template-tag", mb.get("start_param", "start_date")]], "value": iso},
        {"type": ptype, "target": ["variable", ["template-tag", mb.get("end_param", "end_date")]], "value": iso},
    ]


def fetch_card_day(session: requests.Session, base_url: str, card_id: str, day: date,
                   settings: dict, retries: int = 3) -> pd.DataFrame:
    url = f"{base_url.rstrip('/')}/api/card/{card_id}/query/json"
    payload = {"parameters": json.dumps(build_parameters(day, settings))}
    timeout = settings.get("metabase", {}).get("timeout_seconds", 300)
    for attempt in range(retries + 1):
        resp = session.post(url, data=payload, timeout=timeout)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 ** (attempt + 1))
            continue
        if resp.status_code != 200:
            # Status only -- never echo headers (they hold the API key).
            raise RuntimeError(f"Metabase returned HTTP {resp.status_code} for card {card_id} on {day}")
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Metabase query error for card {card_id} on {day}: {str(data['error'])[:200]}")
        return pd.DataFrame(data)
    raise RuntimeError("unreachable")


def select_table(df: pd.DataFrame, columns: dict[str, str]) -> tuple[pd.DataFrame, list[str]]:
    """Keep only mapped columns, rename to canonical names. Returns (frame, missing real names)."""
    present = {r: c for r, c in columns.items() if r in df.columns}
    missing = [r for r in columns if r not in df.columns]
    out = df[list(present)].rename(columns=present).copy()
    # Fill unmapped canonical columns so the schema stays stable.
    for c in columns.values():
        if c not in out.columns:
            out[c] = None
    out = out[list(dict.fromkeys(columns.values()))]
    out = out.dropna(how="all").drop_duplicates().astype("string")
    return out, missing


def pull(start: date, end: date, conn=None, settings=None, column_map=None, session=None) -> dict:
    settings = settings or load_settings()
    column_map = column_map or load_column_map()
    env_vals = require_env("METABASE_URL", "METABASE_API_KEY", "METABASE_CARD_ID") if session is None else {
        "METABASE_URL": "http://test", "METABASE_API_KEY": "", "METABASE_CARD_ID": "0"}
    if session is None:
        session = requests.Session()
        session.headers.update({"X-API-KEY": env_vals["METABASE_API_KEY"]})
    conn = conn or get_conn(settings=settings)

    # group tables by the card they come from
    by_card: dict[str, list[str]] = {}
    for table, spec in column_map.items():
        if table in SKIP_TABLES:
            continue
        card = str(spec.get("card_id") or env_vals["METABASE_CARD_ID"])
        by_card.setdefault(card, []).append(table)

    report = {"days": 0, "rows": {}, "missing_columns": {}}
    for day in daterange(start, end):
        for card, tables in by_card.items():
            raw = fetch_card_day(session, env_vals["METABASE_URL"], card, day, settings)
            for table in tables:
                sub, missing = select_table(raw, column_map[table]["columns"]) if len(raw) else (
                    pd.DataFrame(columns=list(dict.fromkeys(column_map[table]["columns"].values()))).astype("string"),
                    [])
                if missing:
                    report["missing_columns"][table] = missing
                sub["_pull_day"] = day.isoformat()
                replace_rows(conn, f"raw_{table}", sub, "_pull_day", day.isoformat())
                report["rows"][table] = report["rows"].get(table, 0) + len(sub)
        report["days"] += 1
        print(f"  {day} done")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pull Metabase data into the local DuckDB file.")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--skip-normalize", action="store_true")
    a = ap.parse_args(argv)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if end < start:
        raise SystemExit("--end must be on or after --start")
    rep = pull(start, end)
    print(f"Pulled {rep['days']} day(s). Rows per table: {rep['rows']}")
    for t, cols in rep["missing_columns"].items():
        print(f"WARNING: table '{t}': columns not found in Metabase result: {cols} (fix config/column_map.yaml)")
    if not a.skip_normalize:
        from .normalize import build_all
        build_all()
        print("Built unified event table.")


if __name__ == "__main__":
    main()
