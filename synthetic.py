"""Tiny synthetic data builders for tests. All data is made up; no real IDs."""
import pandas as pd

T = pd.Timestamp


def ts(hhmm: str, day="2026-03-02"):
    return T(f"{day} {hhmm}")


def consults(rows):
    """rows: (practitioner, start 'HH:MM', end 'HH:MM', role[, confidence])"""
    out = []
    for i, r in enumerate(rows):
        pid, s, e, role = r[:4]
        conf = r[4] if len(r) > 4 else "high"
        out.append({"practitioner_id": pid, "encounter_id": f"enc{i}", "start": ts(s), "end": ts(e),
                    "role": role, "confidence": conf, "basis": "test"})
    return pd.DataFrame(out)


def shifts(rows):
    """rows: (practitioner, start, end, role[, shift_type])"""
    out = []
    for r in rows:
        pid, s, e, role = r[:4]
        out.append({"practitioner_id": pid, "start": ts(s), "end": ts(e), "role": role,
                    "care_group_id": "g1", "shift_type": r[4] if len(r) > 4 else "clinical"})
    return pd.DataFrame(out)


def queue(start_hhmm, end_hhmm, intake=0, triaged=0, province="ON"):
    mins = pd.date_range(ts(start_hhmm), ts(end_hhmm), freq="min", inclusive="left")
    return pd.DataFrame({"minute": mins, "province": province, "intake_waiting": intake,
                         "triaged_waiting": triaged, "waiting": intake + triaged})
