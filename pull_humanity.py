"""Pull scheduled shifts (with type: on-call / training / admin / clinical) from Humanity.

TODO(confirm API shape): the exact Humanity response for your account was not
available when this was written. `parse_shifts` accepts the commonly seen v2
layout and is covered by a fixture test (tests/fixtures/humanity_shifts.json).
When you can, run:  python -m src.pull_humanity --start ... --end ... --peek
to see the real (name-free) shape, then adjust `parse_shifts` and the fixture.

Only IDs are kept. Employee names that Humanity returns are discarded.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone

import pandas as pd
import requests

from .common import (get_conn, load_people, load_settings, replace_rows,
                     require_env)


def _ts(value) -> str | None:
    """Accept {'timestamp': 123}, {'date': 'YYYY-MM-DD', 'time': 'HH:MM'}, epoch, or ISO string."""
    if value is None:
        return None
    if isinstance(value, dict):
        if value.get("timestamp") not in (None, ""):
            return _ts(value["timestamp"])
        if value.get("date"):
            return _ts(f"{value['date']} {value.get('time', '00:00')}")
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    return str(value)


def classify_shift(text: str, keywords: dict[str, list[str]]) -> str:
    t = (text or "").lower()
    for shift_type, words in keywords.items():
        if any(w in t for w in words):
            return shift_type
    return "clinical"


def parse_shifts(payload, employee_map: dict, keywords: dict) -> pd.DataFrame:
    rows = payload.get("data", []) if isinstance(payload, dict) else (payload or [])
    out = []
    for s in rows:
        label = " ".join(str(s.get(k) or "") for k in ("schedule_name", "position_name", "title", "name", "notes"))
        stype = classify_shift(label, keywords)
        start = _ts(s.get("start_date") or s.get("start") or s.get("start_time"))
        end = _ts(s.get("end_date") or s.get("end") or s.get("end_time"))
        employees = s.get("employees") or ([{"id": s["employee_id"]}] if s.get("employee_id") else [])
        for e in employees:
            eid = str(e.get("id")) if isinstance(e, dict) else str(e)   # name fields ignored on purpose
            out.append({
                "practitioner_id": employee_map.get(eid, f"humanity:{eid}"),
                "start_at": start, "end_at": end, "shift_type": stype,
                "humanity_shift_id": str(s.get("id", "")),
            })
    cols = ["practitioner_id", "start_at", "end_at", "shift_type", "humanity_shift_id"]
    return pd.DataFrame(out, columns=cols).astype("string")


def fetch_shifts(start: date, end: date, settings: dict, api_key: str, session=None) -> dict:
    h = settings.get("humanity", {})
    session = session or requests.Session()
    params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    headers = {}
    if h.get("auth_mode", "header") == "param":
        params["access_token"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = session.get(h["base_url"].rstrip("/") + h.get("shifts_path", "/shifts"),
                       params=params, headers=headers, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Humanity returned HTTP {resp.status_code}")
    return resp.json()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pull Humanity shifts.")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--peek", action="store_true", help="print the first raw shift keys/values (employee names hidden)")
    a = ap.parse_args(argv)
    settings = load_settings()
    key = require_env("HUMANITY_API_KEY")["HUMANITY_API_KEY"]
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    payload = fetch_shifts(start, end, settings, key)
    if a.peek:
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        for r in (rows or [])[:3]:
            r = dict(r)
            if "employees" in r:
                r["employees"] = [{"id": e.get("id")} for e in r["employees"] if isinstance(e, dict)]
            print(r)
    people = load_people().get("humanity_employee_to_practitioner", {}) or {}
    df = parse_shifts(payload, {str(k): v for k, v in people.items()}, settings["humanity"]["shift_type_keywords"])
    conn = get_conn(settings=settings)
    # Each pulled range is stored under one key; re-pulling the same range replaces it.
    key_val = f"{start}_{end}"
    df["_pull_day"] = key_val
    replace_rows(conn, "raw_humanity_shifts", df, "_pull_day", key_val)
    print(f"Stored {len(df)} Humanity shift rows.")
    unmapped = df["practitioner_id"].str.startswith("humanity:").sum()
    if unmapped:
        print(f"WARNING: {unmapped} shift rows have no practitioner mapping (add them to config/people.yaml).")


if __name__ == "__main__":
    main()
