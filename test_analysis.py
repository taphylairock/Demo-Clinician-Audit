import pandas as pd

from src.analysis import on_shift_series, utilization_table
from .synthetic import consults, shifts, ts


def test_on_shift_counts_and_exclusion():
    s = shifts([("a", "09:00", "10:00", "NP"), ("b", "09:30", "10:30", "NP"), ("c", "09:00", "12:00", "MD"),
                ("d", "09:00", "10:00", "NP", "on_call")])
    r = on_shift_series(s, ts("09:00"), ts("11:00"), role="NP")
    assert r[ts("09:15")] == 1 and r[ts("09:45")] == 2 and r[ts("10:15")] == 1 and r[ts("10:45")] == 0
    assert on_shift_series(s, ts("09:00"), ts("11:00"), role="NP", exclude="a")[ts("09:15")] == 0


def test_utilization():
    s = shifts([("a", "09:00", "11:00", "NP")])
    c = consults([("a", "09:00", "09:30", "NP"), ("a", "09:20", "10:00", "NP"), ("a", "10:50", "11:30", "NP")])
    u = utilization_table(c, s).iloc[0]
    assert u.shift_minutes == 120 and u.consult_minutes == 70 and abs(u.pct_busy - 58.3) < 0.1
