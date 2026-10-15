import json
from datetime import date
from pathlib import Path

import pandas as pd

from src import pull_slack
from src.common import get_conn, load_column_map, load_settings
from src.flags import FlagInputs, run_all_flags
from src.normalize import build_all
from src.pull_humanity import classify_shift, parse_shifts
from src.pull_metabase import build_parameters, pull, select_table

FIX = Path(__file__).parent / "fixtures"


def test_humanity_fixture_parser_drops_names_and_classifies():
    kw = load_settings()["humanity"]["shift_type_keywords"]
    df = parse_shifts(json.loads((FIX / "humanity_shifts.json").read_text()), {"101": "prac_001"}, kw)
    assert list(df.practitioner_id) == ["prac_001", "humanity:102", "humanity:103"]
    assert list(df.shift_type) == ["clinical", "on_call", "training"]
    assert "name" not in df.columns and not df.astype(str).apply(lambda c: c.str.contains("REDACTED")).any().any()
    assert df.start_at[0].startswith("2026-03-02")


def test_classify_default_clinical():
    assert classify_shift("Evening NP", {"on_call": ["on call"]}) == "clinical"


def test_slack_normalization_keeps_no_text_or_names():
    audit = [{"date_create": 1772449200, "action": "user_login",
              "actor": {"type": "user", "user": {"id": "U1", "name": "Real Name", "email": "x@y.z"}}}]
    df = pull_slack.normalize_audit_entries(audit, {"U1": "prac_001"})
    assert df.iloc[0].practitioner_id == "prac_001" and list(df.columns) == pull_slack.COLUMNS
    msgs = [{"user": "U2", "ts": "1772449200.1", "text": "secret"}, {"user": "U2", "ts": "1772449300.1", "thread_ts": "1772449200.1"}]
    m = pull_slack.normalize_messages(msgs, {})
    assert list(m.action) == ["message", "reply"] and "text" not in m.columns
    assert m.iloc[0].practitioner_id == "slack:U2"


def test_slack_peek_redaction():
    r = pull_slack.redact({"text": "hi", "actor": {"user": {"id": "U1", "email": "a@b.c", "name": "N"}}})
    assert r["text"] == "<redacted>" and r["actor"]["user"]["email"] == "<redacted>" and r["actor"]["user"]["id"] == "U1"


def test_select_table_drops_unmapped_columns_and_reports_missing():
    raw = pd.DataFrame({"a": [1, 1], "patient_name": ["x", "y"], "b": [2, 2]})
    out, missing = select_table(raw, {"a": "A", "b": "B", "c": "C"})
    assert list(out.columns) == ["A", "B", "C"] and missing == ["c"] and len(out) == 1
    assert "patient_name" not in out.columns


def test_metabase_parameters():
    p = build_parameters(date(2026, 3, 2), load_settings())
    assert [x["value"] for x in p] == ["2026-03-02", "2026-03-02"]


class FakeResp:
    status_code = 200

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def post(self, url, data=None, timeout=None):
        self.calls.append((url, data))
        return FakeResp(self.rows)


def test_end_to_end_pipeline_in_memory():
    rows = [
        {"encounter_id": "e1", "practitioner_id": "np1", "started_at": "2026-03-02T14:00:00Z",
         "ended_at": "2026-03-02T14:30:00Z", "telecall_clinical_state_started_at": "2026-03-02T14:00:00Z",
         "telecall_clinical_state_ended_at": "2026-03-02T14:30:00Z", "locality": "ON",
         "telecall_intake_state_started_at": "2026-03-02T13:30:00Z", "telecall_intake_state_ended_at": "2026-03-02T14:00:00Z",
         "triaged_completed_at": None, "patient_name": "MUST NOT BE STORED",
         "role": "NP", "start_at": "2026-03-02T13:00:00Z", "end_at": "2026-03-02T17:00:00Z", "care_group_id": "g"},
        {"encounter_id": "e2", "practitioner_id": "np1", "started_at": "2026-03-02T15:00:00Z",
         "ended_at": "2026-03-02T15:30:00Z", "telecall_clinical_state_started_at": "2026-03-02T15:00:00Z",
         "telecall_clinical_state_ended_at": "2026-03-02T15:30:00Z", "locality": "ON",
         "telecall_intake_state_started_at": "2026-03-02T14:35:00Z", "telecall_intake_state_ended_at": "2026-03-02T15:00:00Z",
         "triaged_completed_at": None, "patient_name": "MUST NOT BE STORED",
         "role": "NP", "start_at": "2026-03-02T13:00:00Z", "end_at": "2026-03-02T17:00:00Z", "care_group_id": "g"},
    ]
    conn = get_conn(":memory:")
    cmap = load_column_map()
    settings = load_settings()
    rep = pull(date(2026, 3, 2), date(2026, 3, 2), conn=conn, settings=settings, column_map=cmap, session=FakeSession(rows))
    assert rep["days"] == 1
    for t in ("raw_encounters", "raw_worktimes"):
        cols = [r[0] for r in conn.execute(f"describe {t}").fetchall()]
        assert "patient_name" not in cols
    t = build_all(conn, settings, cmap)
    assert len(t["consults"]) == 2 and (t["consults"].role == "NP").all()
    assert t["consults"].start.min() == pd.Timestamp("2026-03-02 09:00")          # UTC -> Toronto
    assert len(t["shifts"]) == 1
    assert t["queue_minute"].waiting.max() == 1
    inp = FlagInputs(t["consults"], t["shifts"], t["queue_minute"], t["encounters"], settings["flags"],
                     settings["shift_type_policy"])
    flags = run_all_flags(inp)
    gap = flags[flags.flag == "gap_between_consults"]
    assert len(gap) == 1 and gap.iloc[0].minutes == 30 and gap.iloc[0].waiting_max == 1
    assert conn.execute("select count(*) from dq_warnings").fetchone()[0] > 0
