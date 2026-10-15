import pandas as pd

from src.normalize import (assign_role, build_consults, build_events, build_shifts, data_quality,
                           to_local)
from .synthetic import ts


def test_to_local_converts_offsets_and_naive():
    r = to_local(["2026-03-02T14:00:00Z", "2026-03-02T09:00:00-05:00", "2026-03-02 09:00:00", None, "garbage"],
                 source_tz="America/Toronto")
    assert r[0] == pd.Timestamp("2026-03-02 09:00")
    assert r[1] == pd.Timestamp("2026-03-02 09:00")
    assert r[2] == pd.Timestamp("2026-03-02 09:00")
    assert pd.isna(r[3]) and pd.isna(r[4])


def test_to_local_dst_summer_offset():
    r = to_local(["2026-07-02T13:00:00Z"])
    assert r[0] == pd.Timestamp("2026-07-02 09:00")        # EDT = UTC-4


def test_to_local_naive_utc_default():
    assert to_local(["2026-03-02 14:00:00"])[0] == pd.Timestamp("2026-03-02 09:00")


def _shifts(rows):
    return pd.DataFrame(rows, columns=["practitioner_id", "start", "end", "role", "care_group_id", "shift_type"])


def test_assign_role_by_shift_then_modal():
    sh = _shifts([("p", ts("09:00"), ts("12:00"), "CM", "g", "clinical"), ("p", ts("13:00"), ts("17:00"), "NP", "g", "clinical")])
    df = pd.DataFrame({"practitioner_id": ["p", "p", "p", "zz"], "t": [ts("10:00"), ts("14:00"), ts("12:30"), ts("10:00")]})
    r = assign_role(df, "t", sh)
    assert list(r) == ["CM", "NP", r.iloc[2], "UNKNOWN"] and r.iloc[2] in ("CM", "NP")


def test_build_shifts_overlays_humanity_type():
    wt = pd.DataFrame({"practitioner_id": ["p"], "start_at": [ts("09:00")], "end_at": [ts("17:00")], "role": ["NP"],
                       "care_group_id": ["g"]})
    hm = pd.DataFrame({"practitioner_id": ["p"], "start_at": [ts("09:00")], "end_at": [ts("17:00")], "shift_type": ["on_call"]})
    assert build_shifts(wt, hm).iloc[0].shift_type == "on_call"
    assert build_shifts(wt, None).iloc[0].shift_type == "clinical"
    assert build_shifts(pd.DataFrame(), hm).iloc[0].role == "UNKNOWN"


def test_cm_consults_use_engaged_seconds_not_span():
    sh = _shifts([("cm1", ts("08:00"), ts("16:00"), "CM", "g", "clinical")])
    part = pd.DataFrame({"encounter_id": ["e1"], "practitioner_id": ["cm1"], "joined_at": [ts("09:00")],
                         "last_checked_in_at": [ts("11:00")], "engaged_secs": [600.0]})
    c = build_consults(pd.DataFrame(), part, sh)
    assert len(c) == 1
    assert c.iloc[0].end - c.iloc[0].start == pd.Timedelta(minutes=10)      # NOT the 2-hour span
    assert c.iloc[0].confidence == "low"
    assert any("LOW confidence" in w["message"] for w in data_quality({"shifts": sh}, {}, c))


def test_cm_message_table_preferred():
    sh = _shifts([("cm1", ts("08:00"), ts("16:00"), "CM", "g", "clinical")])
    part = pd.DataFrame({"encounter_id": ["e1"], "practitioner_id": ["cm1"], "joined_at": [ts("09:00")],
                         "last_checked_in_at": [ts("11:00")], "engaged_secs": [600.0]})
    msgs = pd.DataFrame({"practitioner_id": ["cm1"], "encounter_id": ["e1"], "sent_at": [ts("09:30")]})
    c = build_consults(pd.DataFrame(), part, sh, messages=msgs, message_window_min=2)
    assert c.iloc[0].basis == "message timestamps" and c.iloc[0].confidence == "medium"


def test_np_consults_from_encounters_and_cm_excluded():
    sh = _shifts([("np1", ts("08:00"), ts("16:00"), "NP", "g", "clinical")])
    enc = pd.DataFrame({"encounter_id": ["e1", "e2"], "practitioner_id": ["np1", "np1"],
                        "clinical_started_at": [ts("09:00"), pd.NaT], "clinical_ended_at": [ts("09:20"), pd.NaT],
                        "started_at": [ts("08:55"), ts("10:00")], "ended_at": [ts("09:25"), ts("10:15")]})
    c = build_consults(enc, pd.DataFrame(), sh)
    assert list(c.start) == [ts("09:00"), ts("10:00")]          # second falls back to started_at/ended_at
    assert (c.role == "NP").all() and (c.confidence == "high").all()


def test_events_unified_columns():
    enc = pd.DataFrame({"encounter_id": ["e1"], "practitioner_id": ["p"], "clinical_started_at": [ts("09:00")],
                        "clinical_ended_at": [ts("09:20")]})
    slack = pd.DataFrame({"practitioner_id": ["p"], "ts": [ts("09:05")], "source": ["slack_message"], "action": ["message"]})
    ev = build_events({"encounters": enc, "slack_events": slack, "shifts": _shifts([("p", ts("08:00"), ts("16:00"), "NP", "g", "clinical")])})
    assert list(ev.columns) == ["practitioner_id", "ts", "source", "event_type", "encounter_id"]
    assert {"consult_start", "consult_end", "message", "shift_start", "shift_end"} <= set(ev.event_type)
