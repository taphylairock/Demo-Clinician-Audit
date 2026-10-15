"""Peer-comparison helpers (pure functions) used by the Streamlit app."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .flags import merge_intervals


def on_shift_series(shifts: pd.DataFrame, start, end, role: str | None = None, exclude: str | None = None,
                    shift_types=("clinical",), freq: str = "min") -> pd.Series:
    """Number of distinct providers on a (counted) shift at each minute in [start, end)."""
    grid = pd.date_range(pd.Timestamp(start).floor("min"), pd.Timestamp(end).ceil("min"), freq="min", inclusive="left")
    n = len(grid)
    s = shifts
    if role:
        s = s[s["role"] == role]
    if exclude is not None:
        s = s[s["practitioner_id"] != exclude]
    if shift_types:
        s = s[s["shift_type"].isin(shift_types)]
    diff = np.zeros(n + 1, dtype=np.int64)
    t0 = grid[0] if n else None
    for _, g in s.groupby("practitioner_id"):          # merge a person's overlapping shifts so they count once
        for r in merge_intervals(g.rename(columns={"start": "start", "end": "end"})).itertuples():
            a = int(max(0, (r.start - t0) / pd.Timedelta(minutes=1)))
            b = int(min(n, (r.end - t0) / pd.Timedelta(minutes=1)))
            if b > a:
                diff[a] += 1
                diff[b] -= 1
    res = pd.Series(np.cumsum(diff)[:n], index=grid, name="providers_on_shift")
    return res if freq == "min" else res.resample(freq).max()


def utilization_table(consults: pd.DataFrame, shifts: pd.DataFrame, shift_types=("clinical",)) -> pd.DataFrame:
    """Per provider: scheduled minutes (counted shift types) and consult minutes falling inside them."""
    rows = []
    s = shifts[shifts["shift_type"].isin(shift_types)] if shift_types else shifts
    for pid, sh in s.groupby("practitioner_id"):
        cons = merge_intervals(consults[consults.practitioner_id == pid])
        sched = busy = 0.0
        for x in sh.itertuples():
            sched += (x.end - x.start) / pd.Timedelta(minutes=1)
            for c in cons.itertuples():
                lo, hi = max(c.start, x.start), min(c.end, x.end)
                if hi > lo:
                    busy += (hi - lo) / pd.Timedelta(minutes=1)
        rows.append({"practitioner_id": pid, "role": sh["role"].mode().iloc[0], "shift_minutes": round(sched),
                     "consult_minutes": round(busy), "pct_busy": round(100 * busy / sched, 1) if sched else np.nan})
    return pd.DataFrame(rows, columns=["practitioner_id", "role", "shift_minutes", "consult_minutes", "pct_busy"])
