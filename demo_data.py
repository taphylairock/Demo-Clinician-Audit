"""Make a small SYNTHETIC database so you can try the dashboard before connecting Metabase.

    python -m src.demo_data            (refuses to touch an existing database)
    python -m src.demo_data --force    (replaces it)

Raw tables hold CANONICAL column names (what pull_metabase stores after applying column_map.yaml).
Provider IDs: np_demo (has gaps while patients wait), md_demo, cm_demo. Nothing here is real.
"""
from __future__ import annotations

import argparse

import pandas as pd

from .common import db_path, get_conn, load_settings
from .normalize import build_all

DAY = "2026-03-02"


def _t(hhmm: str) -> str:          # local Toronto time written as UTC-offset-aware ISO (EST = UTC-5)
    return f"{DAY}T{hhmm}:00-05:00"


def make(conn, settings):
    enc, parts = [], []
    n = 0

    def consult(pid, s, e, loc="ON", wait_from=None):
        nonlocal n
        n += 1
        enc.append({"encounter_id": f"e{n}", "practitioner_id": pid, "started_at": _t(s), "ended_at": _t(e),
                    "clinical_started_at": _t(s), "clinical_ended_at": _t(e),
                    "intake_started_at": _t(wait_from) if wait_from else None,
                    "intake_ended_at": _t(s) if wait_from else None,
                    "locality": loc, "language": "EN", "initiated_by": "patient"})
        return f"e{n}"

    for s, e, w in [("09:00", "09:20", None), ("09:25", "09:50", "09:15"), ("10:35", "11:00", "09:50"),
                    ("11:05", "11:30", "10:40"), ("11:35", "12:00", None)]:
        consult("np_demo", s, e, wait_from=w)
    for s, e in [("09:00", "09:30"), ("09:35", "10:05"), ("10:10", "10:40"), ("10:45", "11:15"), ("11:20", "11:50")]:
        consult("md_demo", s, e)
    parts.append({"encounter_id": "e1", "practitioner_id": "cm_demo", "joined_at": _t("09:00"),
                  "last_checked_in_at": _t("12:00"), "engaged_secs": 900})
    work = [{"practitioner_id": p, "start_at": _t("09:00"), "end_at": _t("13:00"), "role": r, "care_group_id": "g1"}
            for p, r in [("np_demo", "NP"), ("md_demo", "MD"), ("cm_demo", "CM")]]
    for name, rows in (("encounters", enc), ("participants", parts), ("worktimes", work)):
        df = pd.DataFrame(rows).astype("string")
        df["_pull_day"] = DAY
        conn.register("_d", df)
        conn.execute(f'create or replace table "raw_{name}" as select * from _d')
        conn.unregister("_d")
    build_all(conn, settings)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    settings = load_settings()
    p = db_path(settings)
    if p.exists() and not a.force:
        raise SystemExit(f"{p} already exists; use --force to replace it with demo data.")
    p.unlink(missing_ok=True)
    make(get_conn(settings=settings), settings)
    print(f"Demo data written to {p}. Now run: streamlit run app.py  (provider np_demo, date {DAY})")


if __name__ == "__main__":
    main()
