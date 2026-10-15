"""Audit flags. Every flag is a plain function: DataFrames in -> DataFrame of flag rows out.
No database, no network, no AI. Times are naive America/Toronto wall-clock.

HOW TO ADD A NEW FLAG LATER
    1. Write  def flag_something(inp: FlagInputs) -> pd.DataFrame  returning FLAG_COLUMNS.
    2. Decorate it with  @register_flag("something").
    3. Add a test in tests/test_flags.py. The app and run_all_flags pick it up automatically.
Planned (from the spec): short/long video call, >1h or very short consults, no/late note, no ownership,
scheduled-appointment outcomes, opened-chart-no-action, ON CM shifts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

FLAG_COLUMNS = ["flag", "practitioner_id", "start", "end", "minutes", "role", "shift_type", "status",
                "waiting_max", "waiting_avg", "waiting_minutes", "confidence", "detail"]

DEFAULT_SETTINGS = {
    "gap_minutes": {"NP": 20, "MD": 20, "CM": 15},
    "empty_block_min": 60,
    "empty_min_waiting_minutes": 15,
    "video_request_values": ["video", "video_call", "vc"],
    "province_aware_queue": True,
}
DEFAULT_POLICY = {"on_call": "suppress", "training": "suppress", "admin": "label", "clinical": "flag"}


@dataclass
class FlagInputs:
    consults: pd.DataFrame                    # practitioner_id, encounter_id, start, end, role, confidence, basis
    shifts: pd.DataFrame                      # practitioner_id, start, end, role, care_group_id, shift_type
    queue: pd.DataFrame                       # minute, province, intake_waiting, triaged_waiting, waiting (sparse)
    encounters: pd.DataFrame | None = None
    flag_settings: dict = field(default_factory=dict)       # the "flags:" block of settings.yaml
    shift_type_policy: dict = field(default_factory=dict)
    provinces: dict = field(default_factory=dict)           # practitioner_id -> list of provinces (empty = all)

    def cfg(self, key):
        return self.flag_settings.get(key, DEFAULT_SETTINGS[key])

    def policy(self, shift_type: str) -> str:
        return {**DEFAULT_POLICY, **self.shift_type_policy}.get(shift_type or "clinical", "flag")


FLAG_REGISTRY: dict[str, Callable[[FlagInputs], pd.DataFrame]] = {}


def register_flag(name: str):
    def deco(fn):
        FLAG_REGISTRY[name] = fn
        return fn
    return deco


# ------------------------------------------------------------------ helpers
def empty_flags() -> pd.DataFrame:
    return pd.DataFrame(columns=FLAG_COLUMNS)


def merge_intervals(df: pd.DataFrame) -> pd.DataFrame:
    """Merge overlapping/touching [start, end] rows (a provider can have concurrent consults).
    Keeps confidence 'low' if any merged row was low."""
    if df.empty:
        return pd.DataFrame(columns=["start", "end", "confidence"])
    d = df.sort_values("start")
    out = []
    cur_s, cur_e, cur_low = None, None, False
    for r in d.itertuples():
        low = getattr(r, "confidence", "high") == "low"
        if cur_s is None:
            cur_s, cur_e, cur_low = r.start, r.end, low
        elif r.start <= cur_e:
            cur_e, cur_low = max(cur_e, r.end), cur_low or low
        else:
            out.append((cur_s, cur_e, "low" if cur_low else "high"))
            cur_s, cur_e, cur_low = r.start, r.end, low
    out.append((cur_s, cur_e, "low" if cur_low else "high"))
    return pd.DataFrame(out, columns=["start", "end", "confidence"])


def waiting_stats(queue: pd.DataFrame, start, end, provinces=None) -> tuple[int, float, int]:
    """(max waiting, average waiting, minutes with anyone waiting) over [start, end).
    The queue table is sparse: a missing minute means nobody was waiting."""
    n = int(max(1, round((end - start) / pd.Timedelta(minutes=1))))
    if queue is None or queue.empty:
        return 0, 0.0, 0
    t0 = start.floor("min")
    q = queue[(queue["minute"] >= t0) & (queue["minute"] < t0 + pd.Timedelta(minutes=n))]
    if provinces:
        q = q[q["province"].isin(provinces)]
    if q.empty:
        return 0, 0.0, 0
    per_min = q.groupby("minute")["waiting"].sum()
    return int(per_min.max()), float(per_min.sum() / n), int((per_min > 0).sum())


def _row(flag, pid, start, end, role, shift_type, status, wstats, confidence, detail):
    return {"flag": flag, "practitioner_id": pid, "start": start, "end": end,
            "minutes": round((end - start) / pd.Timedelta(minutes=1), 1), "role": role,
            "shift_type": shift_type, "status": status, "waiting_max": wstats[0],
            "waiting_avg": round(wstats[1], 2), "waiting_minutes": wstats[2],
            "confidence": confidence, "detail": detail}


def _status(inp: FlagInputs, shift_type: str) -> str | None:
    """None = suppressed (omit). Otherwise the status text for the flag row."""
    p = inp.policy(shift_type)
    if p == "suppress":
        return None
    return "flagged" if p == "flag" else f"labelled: {shift_type} shift"


def _gap_threshold(inp: FlagInputs, role: str) -> float:
    g = inp.cfg("gap_minutes")
    return g.get(role, max(g.values()))


def _clip_to_shifts(shifts: pd.DataFrame, start, end):
    """Yield (shift_row, clipped_start, clipped_end) for each shift overlapping [start, end]."""
    for s in shifts.itertuples():
        a, b = max(start, s.start), min(end, s.end)
        if b > a:
            yield s, a, b


# ------------------------------------------------------------------ FLAG 1
@register_flag("gap_between_consults")
def flag_gap_between_consults(inp: FlagInputs) -> pd.DataFrame:
    """Idle time between two consults, inside a scheduled shift, longer than the role's threshold
    (NP/MD 20 min, CM 15 min). Reports how many patients were waiting in intake/triaged during it."""
    rows = []
    for pid, cons in inp.consults.groupby("practitioner_id"):
        shifts = inp.shifts[inp.shifts.practitioner_id == pid]
        if shifts.empty:
            continue
        merged = merge_intervals(cons)
        for prev, nxt in zip(merged.itertuples(), merged.iloc[1:].itertuples()):
            for s, a, b in _clip_to_shifts(shifts, prev.end, nxt.start):
                role = s.role
                if (b - a) / pd.Timedelta(minutes=1) <= _gap_threshold(inp, role):
                    continue
                status = _status(inp, s.shift_type)
                if status is None:
                    continue
                conf = "low" if "low" in (prev.confidence, nxt.confidence) else "high"
                w = waiting_stats(inp.queue, a, b, inp.provinces.get(pid))
                note = f"{round((b - a) / pd.Timedelta(minutes=1))} min between consults (limit {_gap_threshold(inp, role)})"
                if conf == "low":
                    note += "; LOW confidence (CM engaged time is an estimate)"
                rows.append(_row("gap_between_consults", pid, a, b, role, s.shift_type, status, w, conf, note))
    return pd.DataFrame(rows, columns=FLAG_COLUMNS) if rows else empty_flags()


# ------------------------------------------------------------------ FLAG 2
@register_flag("empty_hours_with_waiting")
def flag_empty_hours_with_waiting(inp: FlagInputs) -> pd.DataFrame:
    """Long idle blocks at the START or END of a scheduled shift (or a whole shift with no consults)
    while patients were waiting. Idle blocks sitting *between* two consults are Flag 1's job."""
    min_block, min_wait = inp.cfg("empty_block_min"), inp.cfg("empty_min_waiting_minutes")
    rows = []
    for s in inp.shifts.itertuples():
        status = _status(inp, s.shift_type)
        if status is None:
            continue
        cons = inp.consults[(inp.consults.practitioner_id == s.practitioner_id)
                            & (inp.consults.start < s.end) & (inp.consults.end > s.start)]
        merged = merge_intervals(cons)
        conf = "low" if (not merged.empty and (merged.confidence == "low").any()) or s.role == "CM" else "high"
        cursor, blocks = s.start, []
        for m in merged.itertuples():
            ms, me = max(m.start, s.start), min(m.end, s.end)
            if ms > cursor:
                blocks.append((cursor, ms, cursor == s.start))   # touches shift start
            cursor = max(cursor, me)
        if cursor < s.end:
            blocks.append((cursor, s.end, True))                 # touches shift end
        for a, b, at_edge in blocks:
            if not at_edge or (b - a) / pd.Timedelta(minutes=1) < min_block:
                continue
            w = waiting_stats(inp.queue, a, b, inp.provinces.get(s.practitioner_id))
            if w[2] < min_wait:
                continue
            note = (f"No consult for {round((b - a) / pd.Timedelta(minutes=1))} min inside the shift; "
                    f"patients waiting in {w[2]} of those minutes (peak {w[0]})")
            if conf == "low":
                note += "; LOW confidence (CM engaged time is an estimate)"
            rows.append(_row("empty_hours_with_waiting", s.practitioner_id, a, b, s.role, s.shift_type,
                             status, w, conf, note))
    return pd.DataFrame(rows, columns=FLAG_COLUMNS) if rows else empty_flags()


# ------------------------------------------------------------------ FLAG 3
@register_flag("no_video_when_requested")
def flag_no_video_when_requested(inp: FlagInputs) -> pd.DataFrame:
    """Patient asked for video but no video call happened. NP/MD only.
    Needs encounters.requested_visit_type and encounters.video_call_started_at (see column_map.yaml)."""
    enc = inp.encounters
    if enc is None or enc.empty or "requested_visit_type" not in enc.columns or "video_call_started_at" not in enc.columns:
        return empty_flags()
    wanted = {v.lower() for v in inp.cfg("video_request_values")}
    e = enc[enc["requested_visit_type"].astype("string").str.lower().str.strip().isin(wanted)
            & enc["video_call_started_at"].isna()]
    if e.empty or inp.consults.empty:
        return empty_flags()
    c = inp.consults[inp.consults.role.isin(["NP", "MD"])]
    m = e[["encounter_id"]].merge(c, on="encounter_id", how="inner")
    rows = []
    for r in m.itertuples():
        rows.append(_row("no_video_when_requested", r.practitioner_id, r.start, r.end, r.role, "clinical", "flagged",
                         (0, 0.0, 0), "high", f"Patient requested video; no video call recorded (encounter {r.encounter_id})"))
    return pd.DataFrame(rows, columns=FLAG_COLUMNS) if rows else empty_flags()


# ------------------------------------------------------------------ runner
def provider_provinces(encounters: pd.DataFrame | None, min_share: float = 0.2) -> dict:
    """Provinces each provider mainly works in (>= min_share of their encounters)."""
    if encounters is None or encounters.empty or "locality" not in encounters.columns:
        return {}
    e = encounters.dropna(subset=["practitioner_id", "locality"])
    out = {}
    for pid, g in e.groupby("practitioner_id"):
        share = g["locality"].value_counts(normalize=True)
        out[pid] = list(share[share >= min_share].index)
    return out


def run_all_flags(inp: FlagInputs, only: list[str] | None = None) -> pd.DataFrame:
    frames = [fn(inp) for name, fn in FLAG_REGISTRY.items() if only is None or name in only]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return empty_flags()
    return pd.concat(frames, ignore_index=True).sort_values(["practitioner_id", "start"]).reset_index(drop=True)
