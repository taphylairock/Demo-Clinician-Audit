"""clinician-audit dashboard.   Run with:  streamlit run app.py

Reads only the local DuckDB file built by `python -m src.pull_metabase ...`. No network, no AI.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src.analysis import on_shift_series, utilization_table
from src.common import db_path, get_conn, load_settings, table_exists
from src.flags import FlagInputs, provider_provinces, run_all_flags

st.set_page_config(page_title="Clinician audit", layout="wide")
settings = load_settings()

# Colours: categorical slots 1-4 + red for flags (validated reference palette).
C = {"shift": "#2a78d6", "shift_muted": "#9aa0a6", "consult": "#eb6834", "slack": "#1baf7a",
     "chr": "#eda100", "flag": "#e34948", "queue": "#6250d6"}
TABLES = ["consults", "shifts", "events", "queue_minute", "encounters", "dq_warnings"]


@st.cache_data(show_spinner="Reading local database...")
def load_tables(db_file: str, mtime: float) -> dict[str, pd.DataFrame]:
    conn = get_conn(db_file, read_only=True)
    try:
        return {t: conn.execute(f'select * from "{t}"').df() for t in TABLES if table_exists(conn, t)}
    finally:
        conn.close()


@st.cache_data(show_spinner="Checking for flags...")
def compute_flags(db_mtime: float, pid: str, start, end, _tables):
    t = _tables
    c, s = t["consults"], t["shifts"]
    c = c[(c.practitioner_id == pid) & (c.end >= start) & (c.start <= end)]
    s = s[(s.practitioner_id == pid) & (s.end >= start) & (s.start <= end)]
    enc = t.get("encounters")
    prov = provider_provinces(enc) if settings["flags"].get("province_aware_queue", True) else {}
    inp = FlagInputs(c, s, t["queue_minute"], enc, settings["flags"], settings["shift_type_policy"], prov)
    return run_all_flags(inp)


db_file = db_path(settings)
if not db_file.exists():
    st.title("Clinician audit")
    st.info("No data yet. In a terminal run:\n\n`python -m src.pull_metabase --start YYYY-MM-DD --end YYYY-MM-DD`\n\n"
            "then reload this page.")
    st.stop()

tables = load_tables(str(db_file), db_file.stat().st_mtime)
if "consults" not in tables or "shifts" not in tables:
    st.warning("The database exists but has not been normalized yet. Run `python -m src.normalize`.")
    st.stop()
mtime = db_file.stat().st_mtime
consults, shifts, events = tables["consults"], tables["shifts"], tables.get("events", pd.DataFrame())
queue = tables.get("queue_minute", pd.DataFrame(columns=["minute", "province", "waiting"]))
dq = tables.get("dq_warnings", pd.DataFrame(columns=["level", "message"]))

# ------------------------------------------------------------------ sidebar
all_ids = sorted(set(shifts.practitioner_id) | set(consults.practitioner_id))
st.sidebar.header("Audit a provider")
typed = st.sidebar.text_input("Provider ID", help="Pseudonymous practitioner ID, exactly as in Metabase.")
pick = st.sidebar.selectbox("...or pick from loaded providers", [""] + all_ids)
pid = (typed or pick).strip()
tmax = pd.concat([shifts["end"], consults["end"]]).max()
default_end = (tmax.date() if pd.notna(tmax) else date.today())
rng = st.sidebar.date_input("Date range", (default_end - timedelta(days=6), default_end))
if isinstance(rng, tuple) and len(rng) == 2:
    d0, d1 = rng
else:
    st.sidebar.info("Pick a start and an end date.")
    st.stop()
start, end = pd.Timestamp(d0), pd.Timestamp(d1) + pd.Timedelta(days=1)
st.sidebar.caption(f"Times are America/Toronto. Data file updated "
                   f"{pd.Timestamp(mtime, unit='s').strftime('%Y-%m-%d %H:%M')} UTC.")

st.title("Clinician audit")
if not pid:
    st.info("Enter or pick a provider ID in the sidebar.")
    st.stop()

p_cons = consults[(consults.practitioner_id == pid) & (consults.end >= start) & (consults.start <= end)]
p_shifts = shifts[(shifts.practitioner_id == pid) & (shifts.end >= start) & (shifts.start <= end)]
if p_cons.empty and p_shifts.empty:
    st.warning(f"No shifts or consults found for '{pid}' between {d0} and {d1}. Check the ID and dates.")
    st.stop()
role = (p_shifts["role"].mode().iloc[0] if not p_shifts.empty else p_cons["role"].mode().iloc[0])

# Visible CM warning (not buried in a tab)
if (p_cons["confidence"] == "low").any() or role == "CM":
    st.warning("**Low confidence (care manager):** no per-message activity table is mapped, so engaged time is "
               "estimated from `secs_of_participants_activity`. The span between joined and last-checked-in is "
               "**not** counted as active time. Treat CM gap results as leads to check, not proof.")

flags = compute_flags(mtime, pid, start, end, tables)
flags_in = flags[(flags.end >= start) & (flags.start <= end)] if not flags.empty else flags

m1, m2, m3, m4 = st.columns(4)
m1.metric("Role", role)
m2.metric("Consults", len(p_cons))
m3.metric("Flags", len(flags_in))
m4.metric("Flagged minutes", int(flags_in["minutes"].sum()) if not flags_in.empty else 0)

tab_tl, tab_flags, tab_peers, tab_dq = st.tabs(["Timeline", "Flags", "Provider vs peers", "Data quality"])


# ------------------------------------------------------------------ timeline
def bars(fig, df, y, color, name, hover, opacity=1.0):
    if df.empty:
        return
    fig.add_trace(go.Bar(base=df["start"], x=(df["end"] - df["start"]).dt.total_seconds() * 1000, y=[y] * len(df),
                         orientation="h", marker=dict(color=color, opacity=opacity), name=name,
                         hovertext=hover, hoverinfo="text", textposition="none", width=0.6, legendgroup=name))


with tab_tl:
    fig = go.Figure()
    if not p_shifts.empty:
        clin = p_shifts[p_shifts.shift_type == "clinical"]
        other = p_shifts[p_shifts.shift_type != "clinical"]
        bars(fig, clin, "Scheduled shift", C["shift"], "Shift", clin.apply(lambda r: f"{r.role} shift {r.start:%H:%M}-{r.end:%H:%M}", axis=1), 0.35)
        bars(fig, other, "Scheduled shift", C["shift_muted"], "Shift (on-call/training/admin)",
             other.apply(lambda r: f"{r.shift_type} shift {r.start:%H:%M}-{r.end:%H:%M}", axis=1), 0.5)
    bars(fig, p_cons, "Consults", C["consult"], "Consult",
         p_cons.apply(lambda r: f"Consult {r.start:%H:%M}-{r.end:%H:%M} ({r.confidence} confidence)", axis=1))
    if not events.empty:
        ev = events[(events.practitioner_id == pid) & (events.ts >= start) & (events.ts <= end)]
        for src_prefix, row, col, nm in (("slack", "Slack", C["slack"], "Slack"), ("chr", "CHR", C["chr"], "CHR")):
            e = ev[ev.source.str.startswith(src_prefix)]
            if not e.empty:
                fig.add_trace(go.Scatter(x=e.ts, y=[row] * len(e), mode="markers", name=nm,
                                         marker=dict(color=col, size=9, line=dict(color="white", width=2)),
                                         text=e.event_type, hovertemplate="%{x|%H:%M} %{text}<extra></extra>"))
    if not flags_in.empty:
        bars(fig, flags_in, "Flagged", C["flag"], "Flagged gap / idle",
             flags_in.apply(lambda r: f"{r.flag}: {r.detail} (waiting peak {r.waiting_max})", axis=1))
        for r in flags_in.itertuples():        # also highlight the flagged span on the shift row
            fig.add_vrect(x0=r.start, x1=r.end, fillcolor=C["flag"], opacity=0.08, line_width=0)
    # zoom to where the data is (a 7-day default range would otherwise squash one busy day)
    spans = [p_shifts[["start", "end"]], p_cons[["start", "end"]]]
    lo = min(x["start"].min() for x in spans if not x.empty)
    hi = max(x["end"].max() for x in spans if not x.empty)
    pad = pd.Timedelta(minutes=30)
    fig.update_layout(barmode="overlay", height=420,
                      xaxis=dict(type="date", range=[max(start, lo - pad), min(end, hi + pad)]),
                      yaxis=dict(categoryorder="array", categoryarray=["Flagged", "CHR", "Slack", "Consults", "Scheduled shift"]),
                      legend=dict(orientation="h", y=1.12), margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width="stretch")
    st.caption("Grey shifts are on-call/training/admin (flags suppressed or labelled). Red = flagged. "
               "Use the legend to show/hide rows; hover for details.")

# ------------------------------------------------------------------ flags
with tab_flags:
    if flags_in.empty:
        st.success("No flags in this range.")
    else:
        kinds = sorted(flags_in.flag.unique())
        c1, c2 = st.columns([3, 1])
        sel = c1.multiselect("Flag types", kinds, default=kinds)
        only_wait = c2.checkbox("Only with patients waiting", value=False)
        view = flags_in[flags_in.flag.isin(sel)]
        if only_wait:
            view = view[view.waiting_max > 0]
        st.dataframe(view, width="stretch", hide_index=True)
        st.download_button("Download CSV", view.to_csv(index=False).encode("utf-8"),
                           file_name=f"flags_{pid}_{d0}_{d1}.csv", mime="text/csv")
        st.caption("`waiting_max` / `waiting_avg` = patients in intake + triaged during the flagged window "
                   "(provider's province(s)). `confidence` low = estimated care-manager time.")

# ------------------------------------------------------------------ peers
with tab_peers:
    pe_shifts = shifts[(shifts.end >= start) & (shifts.start <= end)]
    provs = provider_provinces(tables.get("encounters")).get(pid) if settings["flags"].get("province_aware_queue", True) else None
    q = queue[(queue.minute >= start) & (queue.minute < end)]
    if provs:
        q = q[q.province.isin(provs)]
    peers = on_shift_series(pe_shifts, start, end, role=role, exclude=pid)
    me = on_shift_series(pe_shifts[pe_shifts.practitioner_id == pid], start, end)
    qs = q.groupby("minute")["waiting"].sum().reindex(peers.index, fill_value=0)
    step = "5min" if (end - start) > pd.Timedelta(days=2) else "min"
    if step != "min":
        peers, me, qs = peers.resample(step).max(), me.resample(step).max(), qs.resample(step).max()
    f2 = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08, row_heights=[0.5, 0.5],
                       subplot_titles=(f"Other {role}s on shift (and this provider)", "Patients waiting (intake + triaged)"))
    f2.add_trace(go.Scatter(x=peers.index, y=peers.values, name=f"Peers ({role}) on shift", line=dict(color=C["shift"], width=2, shape="hv")), 1, 1)
    f2.add_trace(go.Scatter(x=me.index, y=me.values, name="This provider on shift", line=dict(color=C["consult"], width=2, shape="hv")), 1, 1)
    f2.add_trace(go.Scatter(x=qs.index, y=qs.values, name="Patients waiting", fill="tozeroy", line=dict(color=C["queue"], width=2, shape="hv")), 2, 1)
    f2.update_layout(height=520, legend=dict(orientation="h", y=1.1), margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(f2, width="stretch")
    st.caption("Two separate scales on purpose (never one chart with two y-axes). "
               + (f"Queue limited to province(s): {', '.join(provs)}." if provs else "Queue includes all provinces."))
    ut = utilization_table(consults[(consults.end >= start) & (consults.start <= end)], pe_shifts)
    if not ut.empty:
        peer_ut = ut[(ut.role == role) & (ut.practitioner_id != pid)]
        mine = ut[ut.practitioner_id == pid]
        a, b = st.columns(2)
        a.metric("This provider: % of shift time in consults", f"{mine.pct_busy.iloc[0]:.0f}%" if not mine.empty else "n/a")
        b.metric(f"Peer median ({role}, {len(peer_ut)} peers)", f"{peer_ut.pct_busy.median():.0f}%" if not peer_ut.empty else "n/a")
        st.caption("Counted shifts only (on-call/training/admin excluded). Peers are shown by ID only.")

# ------------------------------------------------------------------ data quality
with tab_dq:
    if dq.empty:
        st.success("No data-quality warnings recorded.")
    for r in dq.itertuples():
        {"error": st.error, "warning": st.warning}.get(r.level, st.info)(r.message)
