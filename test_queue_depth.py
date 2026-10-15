import pandas as pd

from src.queue_depth import build_queue_intervals, queue_depth
from .synthetic import ts


def test_counts_per_minute_and_province():
    iv = pd.DataFrame([
        ("e1", "intake", ts("09:00"), ts("09:03"), "ON"),
        ("e2", "intake", ts("09:01"), ts("09:02"), "ON"),
        ("e3", "triaged", ts("09:01"), ts("09:04"), "BC"),
    ], columns=["encounter_id", "kind", "start", "end", "province"])
    q = queue_depth(iv)
    on = q[q.province == "ON"].set_index("minute")
    assert on.loc[ts("09:00"), "intake_waiting"] == 1
    assert on.loc[ts("09:01"), "intake_waiting"] == 2
    assert on.loc[ts("09:02"), "intake_waiting"] == 1
    assert ts("09:03") not in on.index
    bc = q[q.province == "BC"].set_index("minute")
    assert bc.loc[ts("09:03"), "triaged_waiting"] == 1 and bc.loc[ts("09:03"), "waiting"] == 1


def test_empty_input():
    assert queue_depth(pd.DataFrame()).empty


def test_intervals_from_state_events_and_fallback():
    enc = pd.DataFrame({"encounter_id": ["e1"], "locality": ["ON"], "clinical_started_at": [ts("09:30")],
                        "ended_at": [ts("10:00")], "intake_started_at": [ts("09:00")], "intake_ended_at": [ts("09:10")],
                        "triaged_completed_at": [ts("09:10")]})
    se = pd.DataFrame({"encounter_id": ["e1", "e1"], "state": ["Intake", "TRIAGED"],
                       "state_started_at": [ts("09:00"), ts("09:10")], "state_ended_at": [ts("09:10"), pd.NaT]})
    settings = {"queue_states": {"intake": ["intake"], "triaged": ["triaged"]}}
    a = build_queue_intervals(se, enc, settings)
    assert set(a.kind) == {"intake", "triaged"}
    assert a[a.kind == "triaged"].iloc[0].end == ts("09:30")        # open end capped at consult start
    b = build_queue_intervals(None, enc, settings)                  # fallback to encounter columns
    assert len(b) == 2 and (b.province == "ON").all()
