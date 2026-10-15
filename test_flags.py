import pandas as pd

from src.flags import (FlagInputs, flag_gap_between_consults, flag_empty_hours_with_waiting,
                       flag_no_video_when_requested, merge_intervals, provider_provinces,
                       run_all_flags, waiting_stats)
from .synthetic import consults, queue, shifts, ts

EMPTY_Q = pd.DataFrame(columns=["minute", "province", "intake_waiting", "triaged_waiting", "waiting"])


def inp(c, s, q=EMPTY_Q, **kw):
    return FlagInputs(consults=c, shifts=s, queue=q, **kw)


# ---- flag 1
def test_gap_over_threshold_flagged_with_waiting_count():
    c = consults([("p1", "09:00", "09:30", "NP"), ("p1", "09:55", "10:20", "NP")])
    s = shifts([("p1", "09:00", "12:00", "NP")])
    q = queue("09:30", "09:55", intake=2, triaged=1)
    r = flag_gap_between_consults(inp(c, s, q))
    assert len(r) == 1
    assert r.iloc[0].minutes == 25
    assert r.iloc[0].waiting_max == 3 and r.iloc[0].waiting_minutes == 25
    assert r.iloc[0].status == "flagged"


def test_gap_at_threshold_not_flagged():
    c = consults([("p1", "09:00", "09:30", "MD"), ("p1", "09:50", "10:20", "MD")])   # exactly 20
    assert flag_gap_between_consults(inp(c, shifts([("p1", "09:00", "12:00", "MD")]))).empty


def test_cm_threshold_is_15():
    c = consults([("p1", "09:00", "09:30", "CM"), ("p1", "09:46", "10:20", "CM")])   # 16 min
    r = flag_gap_between_consults(inp(c, shifts([("p1", "09:00", "12:00", "CM")])))
    assert len(r) == 1


def test_overlapping_consults_do_not_create_gap():
    c = consults([("p1", "09:00", "09:40", "NP"), ("p1", "09:10", "09:20", "NP"), ("p1", "09:50", "10:30", "NP")])
    assert flag_gap_between_consults(inp(c, shifts([("p1", "09:00", "12:00", "NP")]))).empty


def test_gap_outside_shift_ignored_and_clipped():
    c = consults([("p1", "08:00", "08:30", "NP"), ("p1", "10:00", "10:30", "NP")])
    s = shifts([("p1", "09:50", "12:00", "NP")])         # only 10 min of the gap is on shift
    assert flag_gap_between_consults(inp(c, s)).empty


def test_on_call_and_training_suppressed_admin_labelled():
    c = consults([("p1", "09:00", "09:30", "NP"), ("p1", "10:30", "11:00", "NP")])
    for stype, expect in (("on_call", 0), ("training", 0), ("admin", 1), ("clinical", 1)):
        r = flag_gap_between_consults(inp(c, shifts([("p1", "09:00", "12:00", "NP", stype)])))
        assert len(r) == expect, stype
    r = flag_gap_between_consults(inp(c, shifts([("p1", "09:00", "12:00", "NP", "admin")])))
    assert r.iloc[0].status.startswith("labelled")


def test_low_confidence_propagates():
    c = consults([("p1", "09:00", "09:30", "CM", "low"), ("p1", "10:30", "11:00", "CM", "low")])
    r = flag_gap_between_consults(inp(c, shifts([("p1", "09:00", "12:00", "CM")])))
    assert r.iloc[0].confidence == "low" and "LOW" in r.iloc[0].detail


def test_province_filter_on_waiting():
    c = consults([("p1", "09:00", "09:30", "NP"), ("p1", "10:00", "10:30", "NP")])
    s = shifts([("p1", "09:00", "12:00", "NP")])
    q = pd.concat([queue("09:30", "10:00", intake=4, province="BC"), queue("09:30", "10:00", intake=1, province="ON")])
    r = flag_gap_between_consults(inp(c, s, q, provinces={"p1": ["ON"]}))
    assert r.iloc[0].waiting_max == 1


# ---- flag 2
def test_empty_block_at_shift_start_with_waiting():
    c = consults([("p1", "11:00", "11:30", "NP")])
    s = shifts([("p1", "09:00", "12:00", "NP")])
    q = queue("09:00", "11:00", intake=3)
    r = flag_empty_hours_with_waiting(inp(c, s, q))
    assert len(r) == 1 and r.iloc[0].minutes == 120 and r.iloc[0].start == ts("09:00")


def test_empty_block_without_waiting_not_flagged():
    c = consults([("p1", "11:00", "11:30", "NP")])
    assert flag_empty_hours_with_waiting(inp(c, shifts([("p1", "09:00", "12:00", "NP")]))).empty


def test_interior_gap_left_to_flag_1():
    c = consults([("p1", "09:00", "09:30", "NP"), ("p1", "11:00", "12:00", "NP")])
    q = queue("09:30", "11:00", intake=3)
    assert flag_empty_hours_with_waiting(inp(c, shifts([("p1", "09:00", "12:00", "NP")]), q)).empty


def test_whole_shift_no_consults():
    c = consults([("other", "09:00", "09:30", "NP")])
    s = shifts([("p1", "09:00", "12:00", "NP")])
    r = flag_empty_hours_with_waiting(inp(c, s, queue("09:00", "12:00", triaged=2)))
    assert len(r) == 1 and r.iloc[0].practitioner_id == "p1" and r.iloc[0].minutes == 180


def test_empty_suppressed_on_call():
    s = shifts([("p1", "09:00", "12:00", "NP", "on_call")])
    assert flag_empty_hours_with_waiting(inp(consults([("x", "09:00", "09:10", "NP")]), s, queue("09:00", "12:00", intake=5))).empty


# ---- flag 3
def _enc(rows):
    return pd.DataFrame(rows, columns=["encounter_id", "requested_visit_type", "video_call_started_at"])


def test_video_requested_but_no_video():
    c = consults([("p1", "09:00", "09:20", "NP"), ("p2", "10:00", "10:20", "CM")])   # enc0, enc1
    e = _enc([("enc0", "Video", pd.NaT), ("enc1", "video", pd.NaT)])
    r = flag_no_video_when_requested(inp(c, shifts([]), encounters=e))
    assert list(r.encounter_id) if False else len(r) == 1      # CM excluded
    assert r.iloc[0].practitioner_id == "p1"


def test_video_requested_and_happened_not_flagged():
    c = consults([("p1", "09:00", "09:20", "MD")])
    e = _enc([("enc0", "video", ts("09:01"))])
    assert flag_no_video_when_requested(inp(c, shifts([]), encounters=e)).empty


def test_video_flag_off_when_columns_missing():
    c = consults([("p1", "09:00", "09:20", "MD")])
    e = pd.DataFrame({"encounter_id": ["enc0"]})
    assert flag_no_video_when_requested(inp(c, shifts([]), encounters=e)).empty


# ---- helpers / runner
def test_merge_intervals():
    m = merge_intervals(consults([("p", "09:00", "09:30", "NP"), ("p", "09:20", "09:40", "NP", "low"), ("p", "10:00", "10:10", "NP")]))
    assert len(m) == 2 and m.iloc[0].confidence == "low"


def test_waiting_stats_sparse_queue():
    mx, avg, mins = waiting_stats(queue("09:00", "09:10", intake=2), ts("09:00"), ts("09:20"))
    assert mx == 2 and mins == 10 and abs(avg - 1.0) < 1e-9


def test_provider_provinces():
    e = pd.DataFrame({"practitioner_id": ["p"] * 10, "locality": ["ON"] * 8 + ["BC"] * 1 + ["AB"] * 1})
    assert provider_provinces(e) == {"p": ["ON"]}


def test_run_all_flags_combines():
    c = consults([("p1", "09:00", "09:30", "NP"), ("p1", "10:30", "11:00", "NP")])
    s = shifts([("p1", "09:00", "13:00", "NP")])
    q = queue("09:30", "10:30", intake=2)
    r = run_all_flags(inp(c, s, q))
    assert set(r.flag) == {"gap_between_consults"}
