"""Turn raw pulled tables into clean, typed, America/Toronto tables and one unified event table.

All timestamps become *naive local wall-clock time in the configured timezone*
(default America/Toronto). One consequence: during the one-hour daylight-saving
"fall back" overlap, times are ambiguous; the app lists this as a data-quality note.

Outputs in DuckDB (all rebuilt by build_all):
    encounters, participants, state_events, worktimes, appointments, notes,
    encounter_tags, shifts, consults, events, queue_minute, dq_warnings
"""
from __future__ import annotations

import pandas as pd

from .common import (get_conn, load_column_map, load_settings, table_exists)
from .queue_depth import build_queue_intervals, queue_depth

_OFFSET = r"(?:Z|[+-]\d{2}:?\d{2})$"
EVENT_COLUMNS = ["practitioner_id", "ts", "source", "event_type", "encounter_id"]
CONSULT_COLUMNS = ["practitioner_id", "encounter_id", "start", "end", "role", "confidence", "basis"]
SHIFT_COLUMNS = ["practitioner_id", "start", "end", "role", "care_group_id", "shift_type"]


# ----------------------------------------------------------------- time handling
def to_local(values, source_tz: str = "UTC", tz: str = "America/Toronto") -> pd.Series:
    """Parse mixed strings/datetimes to naive local time. Offset-aware values are converted;
    naive values are assumed to be in `source_tz`. Unparseable values become NaT."""
    s = pd.Series(values).reset_index(drop=True)
    strs = s.astype("string")
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    aware = strs.str.contains(_OFFSET, na=False)
    if aware.any():
        p = pd.to_datetime(strs[aware], utc=True, errors="coerce", format="ISO8601")
        out[aware] = p.dt.tz_convert(tz).dt.tz_localize(None).astype("datetime64[ns]")
    naive = (~aware) & strs.notna() & strs.fillna("").ne("")
    if naive.any():
        p = pd.to_datetime(strs[naive], errors="coerce", format="ISO8601")
        p = p.dt.tz_localize(source_tz, ambiguous="NaT", nonexistent="shift_forward")
        out[naive] = p.dt.tz_convert(tz).dt.tz_localize(None).astype("datetime64[ns]")
    return out


def normalize_table(df: pd.DataFrame, source_tz: str, tz: str) -> pd.DataFrame:
    df = df.drop(columns=[c for c in df.columns if c.startswith("_")], errors="ignore").copy()
    for c in df.columns:
        if c.endswith("_at") or c == "ts":
            df[c] = to_local(df[c], source_tz, tz).values
        elif c in ("engaged_secs",):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ----------------------------------------------------------------- shifts / roles
def build_shifts(worktimes: pd.DataFrame, humanity: pd.DataFrame | None) -> pd.DataFrame:
    """Scheduled shifts = worktimes, with shift_type taken from the overlapping Humanity shift.
    If there are no worktimes at all, Humanity shifts are used on their own (role UNKNOWN)."""
    wt = pd.DataFrame(columns=SHIFT_COLUMNS) if worktimes is None or worktimes.empty else worktimes.rename(
        columns={"start_at": "start", "end_at": "end"}).copy()
    if "care_group_id" not in wt.columns:
        wt["care_group_id"] = None
    if "role" not in wt.columns:
        wt["role"] = "UNKNOWN"
    wt["shift_type"] = "clinical"
    hm = None
    if humanity is not None and not humanity.empty:
        hm = humanity.rename(columns={"start_at": "start", "end_at": "end"}).dropna(subset=["start", "end"])
    if hm is not None and not wt.empty:
        for i, row in wt.iterrows():
            h = hm[(hm.practitioner_id == row.practitioner_id) & (hm.start < row["end"]) & (hm["end"] > row["start"])
                   & (hm.shift_type != "clinical")]
            if not h.empty:
                ov = (h[["start"]].assign(e=h["end"]).apply(
                    lambda r: (min(r.e, row["end"]) - max(r.start, row["start"])).total_seconds(), axis=1))
                wt.at[i, "shift_type"] = h.loc[ov.idxmax(), "shift_type"]
    elif hm is not None and wt.empty:
        wt = hm.assign(role="UNKNOWN", care_group_id=None)[SHIFT_COLUMNS]
    return wt[SHIFT_COLUMNS].dropna(subset=["practitioner_id", "start", "end"]).reset_index(drop=True)


def assign_role(df: pd.DataFrame, ts_col: str, shifts: pd.DataFrame) -> pd.Series:
    """Role of each row's practitioner at ts_col: the shift containing it, else their most common role, else UNKNOWN."""
    if shifts is None or shifts.empty or "role" not in shifts.columns:
        return pd.Series("UNKNOWN", index=df.index)
    left = df[["practitioner_id", ts_col]].copy()
    left["_i"] = left.index
    left = left.dropna(subset=[ts_col]).sort_values(ts_col)
    right = shifts[["practitioner_id", "start", "end", "role"]].dropna().sort_values("start")
    m = pd.merge_asof(left, right, left_on=ts_col, right_on="start", by="practitioner_id", direction="backward")
    m.loc[m[ts_col] > m["end"], "role"] = None
    roles = pd.Series(m["role"].values, index=m["_i"].values).reindex(df.index)
    modal = shifts.dropna(subset=["role"]).groupby("practitioner_id")["role"].agg(lambda x: x.mode().iloc[0])
    fallback = df["practitioner_id"].map(modal)
    return roles.fillna(fallback).fillna("UNKNOWN")


# ----------------------------------------------------------------- consults
def build_consults(encounters: pd.DataFrame, participants: pd.DataFrame, shifts: pd.DataFrame,
                   messages: pd.DataFrame | None = None, cm_roles=("CM",), message_window_min: float = 2) -> pd.DataFrame:
    """One row per (provider, consult interval).

    NP/MD: encounter clinical-state start/end (falls back to started_at/ended_at).
    CM:    message-level times if a message table exists (confidence 'medium');
           else joined_at .. joined_at + secs_of_participants_activity, capped by last_check-in
           (confidence 'LOW'). The raw joined->last-checked-in span is NEVER treated as active time.
    """
    parts = []
    if encounters is not None and not encounters.empty:
        e = encounters.copy()
        nat = pd.Series(pd.NaT, index=e.index)
        start = e.get("clinical_started_at", nat).fillna(e.get("started_at", nat))
        end = e.get("clinical_ended_at", nat).fillna(e.get("ended_at", nat))
        c = pd.DataFrame({"practitioner_id": e["practitioner_id"], "encounter_id": e["encounter_id"],
                          "start": start, "end": end}).dropna(subset=["practitioner_id", "start", "end"])
        c = c[c["end"] >= c["start"]]
        c["role"] = assign_role(c, "start", shifts)
        c = c[~c["role"].isin(cm_roles)]
        c["confidence"], c["basis"] = "high", "encounter clinical state"
        parts.append(c)
    cm = None
    if messages is not None and not messages.empty:
        m = messages.dropna(subset=["practitioner_id", "sent_at"]).copy()
        cm = pd.DataFrame({"practitioner_id": m["practitioner_id"], "encounter_id": m.get("encounter_id"),
                           "start": m["sent_at"],
                           "end": m["sent_at"] + pd.Timedelta(minutes=message_window_min)})
        cm["confidence"], cm["basis"] = "medium", "message timestamps"
    elif participants is not None and not participants.empty:
        p = participants.dropna(subset=["practitioner_id", "joined_at"]).copy()
        secs = p["engaged_secs"].fillna(0)
        end = p["joined_at"] + pd.to_timedelta(secs, unit="s")
        if "last_checked_in_at" in p:
            end = end.where(p["last_checked_in_at"].isna() | (end <= p["last_checked_in_at"]), p["last_checked_in_at"])
        cm = pd.DataFrame({"practitioner_id": p["practitioner_id"], "encounter_id": p["encounter_id"],
                           "start": p["joined_at"], "end": end})
        cm = cm[cm["end"] > cm["start"]]
        cm["confidence"], cm["basis"] = "low", "engaged seconds from join time (no per-message data)"
    if cm is not None and not cm.empty:
        cm["role"] = assign_role(cm, "start", shifts)
        cm = cm[cm["role"].isin(cm_roles)]
        parts.append(cm)
    if not parts:
        return pd.DataFrame(columns=CONSULT_COLUMNS)
    return pd.concat(parts, ignore_index=True)[CONSULT_COLUMNS].sort_values(["practitioner_id", "start"]).reset_index(drop=True)


# ----------------------------------------------------------------- unified events
def build_events(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Unified table: practitioner_id, ts, source, event_type, encounter_id."""
    rows = []

    def add(df, ts_col, source, event_type, enc_col="encounter_id"):
        if df is None or df.empty or ts_col not in df.columns:
            return
        d = df.dropna(subset=["practitioner_id", ts_col])
        rows.append(pd.DataFrame({
            "practitioner_id": d["practitioner_id"], "ts": d[ts_col], "source": source,
            "event_type": event_type if isinstance(event_type, str) else d[event_type],
            "encounter_id": d[enc_col] if enc_col in d.columns else None}))

    enc = t.get("encounters")
    add(enc, "clinical_started_at", "encounter", "consult_start")
    add(enc, "clinical_ended_at", "encounter", "consult_end")
    add(enc, "video_call_started_at", "encounter", "video_call_start")
    part = t.get("participants")
    add(part, "joined_at", "participant", "joined")
    add(part, "last_checked_in_at", "participant", "last_checked_in")
    add(t.get("notes"), "note_created_at", "note", "note_created")
    add(t.get("appointments"), "scheduled_start_at", "appointment", "scheduled")
    add(t.get("cm_messages"), "sent_at", "cm_message", "message")
    sh = t.get("shifts")
    if sh is not None and not sh.empty:
        add(sh.rename(columns={"start": "ts"}), "ts", "shift", "shift_start")
        add(sh.rename(columns={"end": "ts"}), "ts", "shift", "shift_end")
    slack = t.get("slack_events")
    if slack is not None and not slack.empty:
        rows.append(pd.DataFrame({"practitioner_id": slack["practitioner_id"], "ts": slack["ts"],
                                  "source": slack["source"], "event_type": slack["action"], "encounter_id": None}))
    chr_ = t.get("chr_events")
    if chr_ is not None and not chr_.empty:
        rows.append(pd.DataFrame({"practitioner_id": chr_["practitioner_id"], "ts": chr_["ts"], "source": "chr",
                                  "event_type": chr_["action"],
                                  "encounter_id": chr_["encounter_id"] if "encounter_id" in chr_ else None}))
    if not rows:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    ev = pd.concat(rows, ignore_index=True)[EVENT_COLUMNS]
    return ev.dropna(subset=["ts"]).sort_values(["practitioner_id", "ts"]).reset_index(drop=True)


# ----------------------------------------------------------------- data quality
def data_quality(t: dict[str, pd.DataFrame], settings: dict, consults: pd.DataFrame | None = None) -> list[dict]:
    w = []

    def warn(level, msg):
        w.append({"level": level, "message": msg})

    for name in ("encounters", "worktimes", "participants"):
        if t.get(name) is None or t[name].empty:
            warn("error", f"No rows in '{name}'. Run the Metabase pull for your date range.")
    enc = t.get("encounters")
    if enc is not None and not enc.empty:
        n = int(enc["practitioner_id"].isna().sum())
        if n:
            warn("warning", f"{n} encounter(s) have no practitioner_id and are ignored.")
        bad = int((enc["clinical_ended_at"] < enc["clinical_started_at"]).sum())
        if bad:
            warn("warning", f"{bad} encounter(s) end before they start (clinical state); skipped.")
        for col in ("requested_visit_type", "video_call_started_at"):
            if col not in enc.columns or enc[col].isna().all():
                warn("info", f"Flag 3 (video requested, no video call) is OFF: '{col}' is not mapped/filled in column_map.yaml.")
                break
    if (t.get("state_events") is None or t["state_events"].dropna(subset=["state"]).empty):
        warn("info", "No state-event table: queue depth is estimated from the encounter intake/triaged columns instead.")
    if consults is not None and not consults.empty and (consults["confidence"] == "low").any():
        warn("warning", "CM activity is LOW confidence: no per-message table is mapped, so engaged time comes from "
                        "secs_of_participants_activity (the joined -> last-checked-in span is NOT treated as active time). "
                        "CM gap results may overstate idle time.")
    if t.get("shifts") is not None and not t["shifts"].empty and (t["shifts"]["shift_type"] == "clinical").all() \
            and (t.get("humanity") is None or t["humanity"].empty):
        warn("info", "No Humanity data loaded: on-call / training / admin shifts cannot be told apart, so gaps in them will be flagged.")
    h = t.get("humanity")
    if h is not None and not h.empty:
        n = int(h["practitioner_id"].astype(str).str.startswith("humanity:").sum())
        if n:
            warn("warning", f"{n} Humanity shift(s) have no practitioner mapping (config/people.yaml).")
    warn("info", f"Timestamps shown in {settings.get('timezone', 'America/Toronto')}; values without an offset are "
                 f"assumed to be {settings.get('source_timezone', 'UTC')}. The 1-hour daylight-saving overlap in November is ambiguous.")
    return w


# ----------------------------------------------------------------- orchestration
def _load_raw(conn, name: str, settings: dict) -> pd.DataFrame | None:
    if not table_exists(conn, f"raw_{name}"):
        return None
    df = conn.execute(f'select * from "raw_{name}"').df()
    df = df.drop_duplicates(subset=[c for c in df.columns if not c.startswith("_")])
    return normalize_table(df, settings.get("source_timezone", "UTC"), settings.get("timezone", "America/Toronto"))


def build_all(conn=None, settings=None, column_map=None) -> dict[str, pd.DataFrame]:
    settings = settings or load_settings()
    column_map = column_map if column_map is not None else load_column_map()
    conn = conn or get_conn(settings=settings)
    t = {}
    for name in ("encounters", "participants", "state_events", "worktimes", "appointments", "notes",
                 "encounter_tags", "cm_messages", "chr_events", "slack_events", "humanity_shifts"):
        t[name] = _load_raw(conn, name, settings)
    t["humanity"] = t.pop("humanity_shifts")
    t["shifts"] = build_shifts(t.get("worktimes"), t.get("humanity"))
    cmcfg = settings.get("cm", {})
    t["consults"] = build_consults(t.get("encounters"), t.get("participants"), t["shifts"],
                                   t.get("cm_messages") if "cm_messages" in column_map else None,
                                   cmcfg.get("role_names", ["CM"]), cmcfg.get("message_window_min", 2))
    t["events"] = build_events(t)
    intervals = build_queue_intervals(t.get("state_events"), t.get("encounters"), settings)
    t["queue_minute"] = queue_depth(intervals)
    t["dq_warnings"] = pd.DataFrame(data_quality(t, settings, t["consults"]), columns=["level", "message"])
    for name in ("encounters", "participants", "state_events", "worktimes", "appointments", "notes",
                 "encounter_tags", "shifts", "consults", "events", "queue_minute", "dq_warnings"):
        df = t.get(name)
        if df is None:
            conn.execute(f'drop table if exists "{name}"')
            continue
        conn.register("_t", df)
        conn.execute(f'create or replace table "{name}" as select * from _t')
        conn.unregister("_t")
    return t


if __name__ == "__main__":
    build_all()
    print("Rebuilt normalized tables.")
