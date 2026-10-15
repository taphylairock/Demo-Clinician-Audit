"""Patients waiting per minute per province (intake + triaged), from state events."""
from __future__ import annotations

import numpy as np
import pandas as pd

INTERVAL_COLUMNS = ["encounter_id", "kind", "start", "end", "province"]
QUEUE_COLUMNS = ["minute", "province", "intake_waiting", "triaged_waiting", "waiting"]


def build_queue_intervals(state_events: pd.DataFrame | None, encounters: pd.DataFrame | None,
                          settings: dict) -> pd.DataFrame:
    """One row per (encounter, waiting-kind, start, end). Prefers state events; falls back to the
    intake/triaged timestamps on the encounter row when no state events were loaded."""
    states = settings.get("queue_states", {"intake": ["intake"], "triaged": ["triaged"]})
    enc = encounters if encounters is not None else pd.DataFrame(columns=["encounter_id"])
    prov = (enc.set_index("encounter_id")["locality"].to_dict() if "locality" in enc.columns else {})
    fallback_end = {}
    if not enc.empty:
        fe = enc.get("clinical_started_at", pd.Series(pd.NaT, index=enc.index)).fillna(enc.get("ended_at"))
        fallback_end = dict(zip(enc["encounter_id"], fe))

    frames = []
    if state_events is not None and not state_events.empty:
        se = state_events.copy()
        se["_s"] = se["state"].astype("string").str.lower().str.strip()
        for kind, names in states.items():
            k = se[se["_s"].isin([n.lower() for n in names])]
            frames.append(pd.DataFrame({"encounter_id": k["encounter_id"], "kind": kind,
                                        "start": k["state_started_at"], "end": k["state_ended_at"]}))
        frames = [f for f in frames if not f.dropna(subset=["start"]).empty]
    if not frames and not enc.empty:
        if "intake_started_at" in enc:
            frames.append(pd.DataFrame({"encounter_id": enc["encounter_id"], "kind": "intake",
                                        "start": enc["intake_started_at"], "end": enc["intake_ended_at"]}))
        if "triaged_completed_at" in enc:
            frames.append(pd.DataFrame({"encounter_id": enc["encounter_id"], "kind": "triaged",
                                        "start": enc["triaged_completed_at"], "end": enc.get("clinical_started_at")}))
    if not frames:
        return pd.DataFrame(columns=INTERVAL_COLUMNS)
    iv = pd.concat(frames, ignore_index=True)
    iv["end"] = iv["end"].fillna(iv["encounter_id"].map(fallback_end))
    iv["province"] = iv["encounter_id"].map(prov).fillna("UNKNOWN")
    iv = iv.dropna(subset=["start", "end"])
    iv = iv[iv["end"] > iv["start"]]
    return iv[INTERVAL_COLUMNS].reset_index(drop=True)


def queue_depth(intervals: pd.DataFrame) -> pd.DataFrame:
    """Per-minute counts. A patient counts in every minute their interval touches
    (start floored to the minute, end rounded up)."""
    if intervals is None or intervals.empty:
        return pd.DataFrame(columns=QUEUE_COLUMNS)
    t0 = intervals["start"].min().floor("min")
    t1 = intervals["end"].max().ceil("min")
    n = int((t1 - t0) / pd.Timedelta(minutes=1)) + 1
    minutes = pd.date_range(t0, periods=n, freq="min")
    out = []
    for prov, g in intervals.groupby("province"):
        cols = {}
        for kind in ("intake", "triaged"):
            k = g[g["kind"] == kind]
            diff = np.zeros(n + 1, dtype=np.int64)
            if not k.empty:
                s = ((k["start"].dt.floor("min") - t0) / pd.Timedelta(minutes=1)).astype(int).to_numpy()
                e = ((k["end"].dt.ceil("min") - t0) / pd.Timedelta(minutes=1)).astype(int).to_numpy()
                np.add.at(diff, s, 1)
                np.add.at(diff, e, -1)
            cols[f"{kind}_waiting"] = np.cumsum(diff)[:n]
        df = pd.DataFrame({"minute": minutes, "province": prov, **cols})
        out.append(df[(df.intake_waiting > 0) | (df.triaged_waiting > 0)])
    res = pd.concat(out, ignore_index=True)
    res["waiting"] = res["intake_waiting"] + res["triaged_waiting"]
    return res[QUEUE_COLUMNS].sort_values(["province", "minute"]).reset_index(drop=True)
