"""CivicPulse Streamlit dashboard: Supabase Gold data plus cited RAG answers."""
from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

from civicpulse import config
from civicpulse.rag import generator
from civicpulse.rag.index import open_collection
from civicpulse.rag.retriever import _find_city_and_issue

st.set_page_config(page_title="CivicPulse", page_icon="📍", layout="wide")


def _secret(name: str, default: str = "") -> str:
    """Read a setting from Streamlit secrets first, then environment variables."""
    try:
        value = st.secrets.get(name, default)
    except FileNotFoundError:
        value = default
    return str(value or os.getenv(name, default)).strip()


def _supabase_settings() -> tuple[str, str]:
    url = _secret("SUPABASE_URL").rstrip("/")
    key = _secret("SUPABASE_PUBLISHABLE_KEY") or _secret("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError(
            "Configure SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY in "
            ".streamlit/secrets.toml (local) or Streamlit app Secrets (deployment)."
        )
    return url, key


def _supabase_get(table: str, *, select: str, order: str, limit: int,
                  filters: dict[str, str] | None = None) -> list[dict]:
    """Read a bounded page from Supabase PostgREST using the app's read-only key."""
    url, key = _supabase_settings()
    params: dict[str, str | int] = {"select": select, "order": order, "limit": limit}
    params.update(filters or {})
    response = requests.get(
        f"{url}/rest/v1/{table}",
        params=params,
        # Supabase Publishable keys are API keys, not JWT access tokens. The API gateway
        # maps them to the anon role; RLS policies decide which rows may be read.
        headers={"apikey": key},
        timeout=30,
    )
    if not response.ok:
        detail = response.text[:700]
        raise RuntimeError(f"Supabase read of {table} failed (HTTP {response.status_code}): {detail}")
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Supabase returned an unexpected response for {table}.")
    return payload


@st.cache_data(ttl=300, show_spinner=False)
def load_recent_counts(limit: int = 500) -> pd.DataFrame:
    fields = "row_id,hour_utc,city,issue_type,n_reports,n_sources,n_imputed"
    records = _supabase_get(
        "hourly_issue_counts", select=fields, order="hour_utc.desc", limit=limit
    )
    frame = pd.DataFrame(records)
    if not frame.empty:
        frame["hour_utc"] = pd.to_datetime(frame["hour_utc"], utc=True, errors="coerce")
        frame["n_reports"] = pd.to_numeric(frame["n_reports"], errors="coerce")
    return frame


@st.cache_data(ttl=300, show_spinner=False)
def load_recent_forecasts(limit: int = 1000) -> pd.DataFrame:
    fields = (
        "forecast_run_id,generated_at_utc,forecast_for_utc,city,issue_type,"
        "expected_reports,lower_95,upper_95,training_cutoff_utc,training_rows,alpha"
    )
    records = _supabase_get(
        "poisson_forecasts", select=fields, order="generated_at_utc.desc", limit=limit
    )
    frame = pd.DataFrame(records)
    if not frame.empty:
        for column in ("generated_at_utc", "forecast_for_utc", "training_cutoff_utc"):
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
        for column in ("expected_reports", "lower_95", "upper_95", "training_rows", "alpha"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        # Newest run first: old rows from earlier scheduled runs must not appear current.
        latest_run = frame["generated_at_utc"].max()
        frame = frame[frame["generated_at_utc"] == latest_run].copy()
    return frame


def _gold_evidence(question: str) -> list[dict]:
    """Fetch exact observed counts and latest forecast rows from Supabase."""
    city, issue = _find_city_and_issue(question)
    if city is None and issue is None:
        return []
    filters: dict[str, str] = {}
    if city:
        filters["city"] = f"eq.{city}"
    if issue:
        filters["issue_type"] = f"eq.{issue}"
    rows = _supabase_get(
        "hourly_issue_counts", select="row_id,hour_utc,city,issue_type,n_reports",
        order="hour_utc.desc", limit=5, filters=filters,
    )
    evidence = []
    for row in rows:
        row_id, hour = row["row_id"], row["hour_utc"]
        evidence.append({
            "text": (
                f"Observed CivicPulse news-report count: {row['n_reports']}. City: {row['city']}. "
                f"Issue: {row['issue_type']}. Hour UTC: {hour}. This is a report count, "
                "not a confirmed incident count."
            ),
            "metadata": {
                "evidence_type": "gold_row", "title": "CivicPulse observed hourly report count",
                "publisher": "CivicPulse Gold warehouse in Supabase",
                "source_url": f"supabase://hourly_issue_counts/{row_id}",
                "table_name": "hourly_issue_counts", "row_id": row_id,
                "hour_utc": hour, "city": row["city"], "issue_type": row["issue_type"],
            },
            "distance": None,
        })
    if city and issue:
        forecasts = _supabase_get(
            "poisson_forecasts",
            select=("forecast_run_id,generated_at_utc,forecast_for_utc,city,issue_type,"
                    "expected_reports,lower_95,upper_95,training_cutoff_utc,training_rows,alpha"),
            order="generated_at_utc.desc,forecast_for_utc.asc", limit=100,
            filters={"city": f"eq.{city}", "issue_type": f"eq.{issue}"},
        )
        if forecasts:
            newest = max(row["generated_at_utc"] for row in forecasts)
            for row in forecasts:
                if row["generated_at_utc"] != newest:
                    continue
                row_id = f"{row['forecast_run_id']}_{row['city']}_{row['issue_type']}_{row['forecast_for_utc']}"
                evidence.append({
                    "text": (
                        "CivicPulse Poisson forecast of future hourly news-report counts; "
                        "this is an estimate, not a confirmed incident count. "
                        f"City: {row['city']}. Issue: {row['issue_type']}. "
                        f"Forecast hour UTC: {row['forecast_for_utc']}. "
                        f"Expected reports: {float(row['expected_reports']):.6f}; "
                        f"95% prediction interval: {row['lower_95']} to {row['upper_95']}. "
                        f"Generated UTC: {row['generated_at_utc']}; training cutoff UTC: "
                        f"{row['training_cutoff_utc']}; training rows: {row['training_rows']}; "
                        f"alpha: {row['alpha']}."
                    ),
                    "metadata": {
                        "evidence_type": "model_forecast", "title": "CivicPulse Poisson hourly forecast",
                        "publisher": "CivicPulse hourly forecasting pipeline; Supabase",
                        "source_url": f"supabase://poisson_forecasts/{row_id}",
                        "table_name": "poisson_forecasts", "row_id": row_id,
                        "hour_utc": row["forecast_for_utc"], "city": row["city"],
                        "issue_type": row["issue_type"],
                    },
                    "distance": None,
                })
    return evidence


def retrieve_app_evidence(question: str, top_k: int = 5) -> list[dict]:
    """Search the deployed Chroma index and append Supabase-backed Gold evidence."""
    index_path = _secret("RAG_VECTORSTORE_DIR")
    if index_path:
        config.RAG_VECTORSTORE_DIR = Path(index_path)
    _, collection = open_collection()
    if collection.count() == 0:
        raise RuntimeError(
            "The RAG document index is empty. Build it with `python -m civicpulse rag-index`, "
            "then include the generated index in the deployment."
        )
    hits = collection.query(
        query_texts=[question], n_results=min(top_k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )
    evidence = [
        {"text": text, "metadata": metadata, "distance": float(distance)}
        for text, metadata, distance in zip(
            hits["documents"][0], hits["metadatas"][0], hits["distances"][0]
        )
    ]
    evidence.extend(_gold_evidence(question))
    return evidence

@st.cache_data(show_spinner=False)
def load_app_artifact(filename: str) -> dict:
    """Load compact, versioned project examples bundled with the application."""
    path = Path(__file__).resolve().parent / "app_data" / filename
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@st.cache_data(ttl=300, show_spinner=False)
def load_freshness() -> pd.DataFrame:
    """Load source freshness if the optional Supabase table is configured and readable."""
    records = _supabase_get(
        "data_freshness",
        select="source,status,last_fetch_utc,last_published_utc,rows,checked_utc",
        order="checked_utc.desc",
        limit=20,
    )
    frame = pd.DataFrame(records)
    if not frame.empty:
        for column in ("last_fetch_utc", "last_published_utc", "checked_utc"):
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    return frame


@st.cache_data(show_spinner=False)
def get_rag_chunk_count() -> int:
    """Read the packaged Chroma collection size without loading its embedding model."""
    index_path = _secret("RAG_VECTORSTORE_DIR")
    if index_path:
        config.RAG_VECTORSTORE_DIR = Path(index_path)
    import chromadb
    client = chromadb.PersistentClient(path=str(config.RAG_VECTORSTORE_DIR))
    return int(client.get_collection("civicpulse_evidence").count())


def _records_json(record: dict) -> str:
    """Format scalar sample rows cleanly for the JSON panels."""
    def default(value):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    return json.dumps(record, ensure_ascii=False, indent=2, default=default)


def _external_link(label: str, url: str, note: str = "") -> str:
    suffix = f"<span>{note}</span>" if note else ""
    return f'<a class="source-link" href="{url}" target="_blank" rel="noopener">{label} ↗</a>{suffix}'


def _show_sample(label: str, sample: dict | None) -> None:
    """Show only the short, one-record example; never expand or dump a source archive."""
    if not sample:
        st.info(f"No packaged {label} sample is available in this build.")
        return
    with st.expander(f"View one {label} sample as JSON", expanded=False):
        st.code(_records_json(sample), language="json", line_numbers=False)
        st.caption("One compact sample record only. Full source payloads are not displayed.")


# Polished, responsive styling. The colors follow the project methodology board.
st.markdown("""
<style>
:root { --navy:#101d32; --ink:#1d2a3d; --muted:#68768c; --paper:#f1f4fa; --line:#dce3ee;
        --blue:#2463d3; --green:#168058; --purple:#5636ad; --teal:#087b86; --orange:#b44516; --pink:#ad1457; }
html, body, [class*="css"] { font-family:Inter,"Segoe UI",ui-sans-serif,system-ui,sans-serif; }
[data-testid="stAppViewContainer"] { background:var(--paper); }
[data-testid="stHeader"] { background:transparent; }
.block-container { max-width:1440px; padding:1.5rem clamp(1rem,3vw,2.8rem) 3rem; }
.hero { background:linear-gradient(112deg,#101d32 0%,#172b49 70%,#26325a 100%); border-radius:24px;
  color:#f8faff; padding:clamp(1.5rem,4vw,3rem); margin:0 0 1.4rem; box-shadow:0 18px 48px #172b4920; }
.eyebrow { color:#a8bad7; font-size:.72rem; font-weight:700; letter-spacing:.18em; text-transform:uppercase; margin-bottom:.7rem; }
.hero h1 { color:white; font:800 clamp(2rem,5vw,3.5rem)/1.06 Inter,"Segoe UI",sans-serif; letter-spacing:-.045em; margin:0; }
.hero p { color:#c0ccdc; max-width:760px; font-size:1.02rem; line-height:1.65; margin:.9rem 0 0; }
.hero-grid { display:grid; grid-template-columns:repeat(4,minmax(115px,1fr)); gap:.7rem; margin-top:1.5rem; }
.hero-stat { padding:1rem 1.1rem; border-radius:15px; background:#ffffff0d; border:1px solid #ffffff12; }
.hero-stat b { display:block; color:#fff; font:800 1.35rem Inter,"Segoe UI",sans-serif; }
.hero-stat small { color:#aab9cf; font-size:.75rem; }
.stage-note { border-left:4px solid var(--blue); background:#fff; border-radius:0 14px 14px 0; padding:1rem 1.1rem; margin:.7rem 0 1rem; box-shadow:0 5px 18px #1e35510c; }
.stage-note h3 { color:var(--ink); font:700 1.03rem Inter,"Segoe UI",sans-serif; margin:0 0 .35rem; }
.stage-note p { color:var(--muted); line-height:1.6; margin:0; }
.info-card { height:100%; background:#fff; border:1px solid var(--line); border-radius:17px; padding:1.05rem 1.15rem; box-shadow:0 5px 20px #1e35510a; }
.info-card .kicker { color:#75839a; text-transform:uppercase; letter-spacing:.1em; font-size:.67rem; font-weight:700; }
.info-card h3 { color:var(--ink); font:700 1rem Inter,"Segoe UI",sans-serif; margin:.35rem 0 .45rem; }
.info-card p { color:var(--muted); font-size:.9rem; line-height:1.55; margin:0; }
.source-link { color:#2860bd; font-weight:700; text-decoration:none; display:inline-block; margin:.25rem .3rem .25rem 0; }
.source-link:hover { text-decoration:underline; }
.source-link + span { display:block; color:#78859a; font-size:.78rem; margin-bottom:.5rem; }
.step-flow { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.65rem; margin:1rem 0; }
.step { background:#fff; border:1px solid var(--line); border-radius:14px; padding:.9rem; }
.step b { color:var(--blue); font-size:.75rem; letter-spacing:.06em; text-transform:uppercase; }
.step strong { display:block; color:var(--ink); font-family:'Manrope',sans-serif; margin:.25rem 0; }
.step span { color:var(--muted); font-size:.82rem; line-height:1.45; }
.callout { background:#fff6e7; border:1px solid #f3dfbd; border-radius:14px; padding:.9rem 1rem; color:#694d27; font-size:.88rem; line-height:1.55; margin:.8rem 0; }
.dark-callout { background:#14233b; border-radius:16px; padding:1.1rem 1.2rem; color:#eaf0fa; line-height:1.55; }
.dark-callout strong { color:#fff; }
section[data-testid="stSidebar"] { background:linear-gradient(180deg,#101d32 0%,#142743 100%); border-right:1px solid #263955; }
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h1,
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h2,
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h3,
section[data-testid="stSidebar"] label { color:#e9f0fa !important; }
section[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] { gap:.32rem; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label { background:#ffffff08; border:1px solid #ffffff0c;
  border-radius:11px; padding:.57rem .72rem; transition:background .18s ease,border .18s ease; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label:hover { background:#ffffff16; border-color:#ffffff30; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) { background:#2463d3; border-color:#73a0fa; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label p { font-weight:700; font-size:.9rem; }
section[data-testid="stSidebar"] [data-testid="stButton"] button { border:1px solid #587094; background:#1d3557; color:white; border-radius:11px; }
section[data-testid="stSidebar"] [data-testid="stButton"] button:hover { background:#28518a; border-color:#9bb8e8; color:white; }
section[data-testid="stSidebar"] hr { border-color:#ffffff22; }
section[data-testid="stSidebar"] small, section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color:#aebdd1 !important; }
div[data-testid="stMetric"] { background:#fff; border:1px solid var(--line); border-radius:15px; padding:.85rem 1rem; }
div[data-testid="stMetricLabel"] p { color:#69778c; font-weight:600; }
div[data-testid="stMetricValue"] { color:var(--ink); font-family:Inter,"Segoe UI",sans-serif; }
div[data-testid="stChatMessage"] { border:1px solid var(--line); border-radius:18px; box-shadow:0 4px 16px #1e35510a; }
@media (max-width:760px) { .hero-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .step-flow { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .block-container { padding-left:.8rem; padding-right:.8rem; }
  section[data-testid="stSidebar"] { min-width:250px; }
}
@media (max-width:420px) { .hero-grid,.step-flow { grid-template-columns:1fr 1fr; gap:.45rem; }
  .hero-stat { padding:.75rem; } }
</style>
""", unsafe_allow_html=True)

try:
    counts = load_recent_counts()
    forecasts = load_recent_forecasts()
    data_error = ""
except Exception as exc:
    counts, forecasts = pd.DataFrame(), pd.DataFrame()
    data_error = str(exc)

artifacts = load_app_artifact("pipeline_snapshot.json")
source_examples = load_app_artifact("source_samples.json").get("samples", {})
freshness = pd.DataFrame()
if not data_error:
    try:
        freshness = load_freshness()
    except Exception:
        # Freshness is an optional table; dashboard counts and forecasts still work without it.
        freshness = pd.DataFrame()

latest_count = counts["hour_utc"].max() if not counts.empty else pd.NaT
latest_forecast = forecasts["generated_at_utc"].max() if not forecasts.empty else pd.NaT
recent_report_total = int(counts["n_reports"].fillna(0).sum()) if not counts.empty else 0

NAV_ITEMS = (
    "01  Problem",
    "02  Sources",
    "03  Data pipeline",
    "04  Forecast model",
    "05  RAG knowledge",
    "06  Generative AI",
    "07  Ask CivicPulse",
)

with st.sidebar:
    st.markdown("<div class='eyebrow'>CIVICPULSE · PROJECT GUIDE</div>", unsafe_allow_html=True)
    st.markdown("## Navigate the story")
    st.caption("Follow each stage from the public-service problem to cited answers.")
    selected_stage = st.radio("PROJECT STAGES", NAV_ITEMS, index=0, label_visibility="visible")
    st.markdown("---")
    if st.button("↻ Refresh Supabase data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    if not data_error and not pd.isna(latest_count):
        st.markdown(f"**Latest observed hour**  \n{latest_count.strftime('%d %b %Y · %H:%M UTC')}")
    st.caption("Counts and forecasts are read from Supabase. Data refreshes here after the hourly workflow sync; this button clears the five-minute app cache.")

active_number = NAV_ITEMS.index(selected_stage) + 1
st.markdown(f"""
<section class="hero">
  <div class="eyebrow">STAGE {active_number:02d} / 07 · CIVICPULSE METHODOLOGY</div>
  <h1>From data to<br>grounded insight.</h1>
  <p>A transparent, hourly pipeline for monitoring public-service reports in Pakistan — from raw source records and quality checks to forecasts, retrieval, and cited answers.</p>
  <div class="hero-grid">
    <div class="hero-stat"><b>7 stages</b><small>end-to-end methodology</small></div>
    <div class="hero-stat"><b>2 gates</b><small>data quality checks</small></div>
    <div class="hero-stat"><b>Hourly</b><small>pipeline refresh target</small></div>
    <div class="hero-stat"><b>Evidence-first</b><small>retrieval + GenAI</small></div>
  </div>
</section>
""", unsafe_allow_html=True)

if data_error:
    st.warning(f"Live Supabase data is temporarily unavailable: {data_error}")
    st.caption("The methodology, source examples, and packaged local pipeline snapshot are still available below.")

# 01 — Problem framing
if selected_stage == NAV_ITEMS[0]:
    st.markdown('<div class="stage-note"><h3>01 · Frame the problem</h3><p>Public-service disruptions affect daily life, but information is spread across news reports, agency notices, and weather updates. CivicPulse organizes those signals by issue, place, and time.</p></div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown('<div class="info-card"><div class="kicker">The need</div><h3>One view of civic signals</h3><p>Water, electricity, mobility, and municipal reports are difficult to compare when they sit in separate articles and feeds.</p></div>', unsafe_allow_html=True)
    with c2:
        st.markdown('<div class="info-card"><div class="kicker">Why this problem</div><h3>It affects everyday decisions</h3><p>Residents, service teams, and planners benefit from a clearer view of what is being reported and where information is thin.</p></div>', unsafe_allow_html=True)
    with c3:
        st.markdown('<div class="info-card"><div class="kicker">What CivicPulse provides</div><h3>Evidence, not alerts</h3><p>Hourly report counts, experimental count forecasts, and reference guidance with traceable sources.</p></div>', unsafe_allow_html=True)
    st.markdown("#### Project scope")
    st.write("The system classifies public news reports into **Electricity, Water, Mobility, and Municipal** issues. It supports exploratory monitoring in Pakistan; city coverage and source freshness vary. A report count is not a verified incident count.")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Issue groups", "4")
    k2.metric("Configured city names", "18")
    k3.metric("Data refresh target", "1 hour")
    k4.metric("Intended use", "Monitoring")
    st.markdown('<div class="callout"><b>Why show this?</b> To help users interpret every graph and forecast with the right context: the product summarizes reporting activity. It does not confirm incidents, replace official warnings, or measure service quality directly.</div>', unsafe_allow_html=True)

# 02 — Sources and bounded raw samples
if selected_stage == NAV_ITEMS[1]:
    st.markdown('<div class="stage-note" style="border-color:var(--green)"><h3>02 · Sources and document data</h3><p>Three structured feeds supply the analytics pipeline. Official websites and project documents supply reference guidance for RAG. Each card links to the source; the JSON panels show one short fetched-record example.</p></div>', unsafe_allow_html=True)
    st.markdown("#### Data feeds")
    feed_cols = st.columns(3)
    feed_info = [
        ("FreeNewsAPI", "Recent Pakistan news articles", "https://freenewsapi.ai/", "Polled hourly with an overlap window. Civic issue mentions are inferred from article title and URL."),
        ("GDELT 2.0", "Historical event records", "https://www.gdeltproject.org/", "Sampled 15-minute exports are filtered to Pakistan action locations. It is not a complete archive."),
        ("Open-Meteo", "Historical and forecast weather", "https://open-meteo.com/", "Temperature, precipitation, and wind are model features; weather does not label an incident."),
    ]
    for col, (name, purpose, link, detail) in zip(feed_cols, feed_info):
        with col:
            st.markdown(f'<div class="info-card"><div class="kicker">Input feed</div><h3>{name}</h3><p><b>{purpose}</b><br>{detail}</p>{_external_link("Open source",link)}</div>', unsafe_allow_html=True)
            _show_sample(name, source_examples.get(name))
    source_volume = pd.DataFrame({"Bronze rows": [
        artifacts.get("bronze", {}).get("FreeNewsAPI", 0),
        artifacts.get("bronze", {}).get("GDELT", 0),
    ]}, index=["FreeNewsAPI", "GDELT"])
    st.markdown("#### Feed volume · last packaged local run")
    st.bar_chart(source_volume, color="#168058")
    st.caption("Counts are from the last local warehouse snapshot shown in the data-pipeline tab. The hourly deployment writes Gold/forecast outputs to Supabase; it does not publish raw Bronze files.")
    st.markdown("#### RAG reference sources · links only")
    refs = [
        ("Pakistan Meteorological Department", "https://weather.gov.pk/", "Forecasts, rain and heat guidance"),
        ("Karachi Water & Sewerage Corporation", "https://www.kwsc.gos.pk/", "Water-service guidance and notices"),
        ("K-Electric", "https://ke.com.pk/", "Electricity-service guidance and notices"),
        ("Pakistan Citizen Portal", "https://citizenportal.gov.pk/", "How citizens submit service complaints"),
        ("NDMA", "https://ndma.gov.pk/", "Disaster preparedness and safety reference"),
    ]
    ref_cols = st.columns(3)
    for index, (name, link, purpose) in enumerate(refs):
        with ref_cols[index % 3]:
            st.markdown(f'<div class="info-card"><div class="kicker">Reference · RAG</div><h3>{name}</h3><p>{purpose}</p>{_external_link("Visit reference",link)}</div>', unsafe_allow_html=True)
    st.caption("The RAG index also includes the local NDMA.pdf and PCP.pdf files supplied with this project, plus CivicPulse model/data documentation. The app shows source links and short excerpts only, not full reference documents.")
    if not freshness.empty:
        st.markdown("#### Latest source status synced to Supabase")
        st.dataframe(freshness[[c for c in ("source", "status", "last_fetch_utc", "rows") if c in freshness]], use_container_width=True, hide_index=True)

# 03 — preprocessing and layers
if selected_stage == NAV_ITEMS[2]:
    st.markdown('<div class="stage-note" style="border-color:var(--purple)"><h3>03 · Warehouse and data-quality agent</h3><p>Raw records are preserved, cleaned into a shared shape, checked, and grouped into analysis-ready tables. Failed checks can stop a new build so the last trusted Gold output remains available.</p></div>', unsafe_allow_html=True)
    st.markdown('<div class="step-flow"><div class="step"><b>Bronze · raw</b><strong>Keep the original</strong><span>Append source payloads and retain file hashes and manifest provenance for traceability.</span></div><div class="step"><b>Gate 1 → Silver</b><strong>Clean and standardize</strong><span>Normalize text, URLs, aliases, timestamps, and fields; deduplicate and mark imputations.</span></div><div class="step"><b>Gate 2 → Gold</b><strong>Group for analysis</strong><span>Aggregate issue reports by UTC hour, city, and issue; join weather features where available.</span></div><div class="step"><b>Evidence</b><strong>Keep the trail</strong><span>Quality actions, row IDs, source manifests, and file paths explain where data came from.</span></div></div>', unsafe_allow_html=True)
    snap = artifacts
    bronze = snap.get("bronze", {})
    silver = snap.get("silver", {})
    gold = snap.get("gold", {})
    weather_stats = snap.get("weather", {})
    st.caption(f"Layer detail snapshot: last successful local build at {snap.get('finished_utc','unknown')} UTC · run `{snap.get('run_id','unknown')}` · {snap.get('status','unknown')}. The deployment does not receive Bronze/Silver rows; its live Gold counts come from Supabase.")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Bronze raw rows", f"{bronze.get('total',0):,}", help="Packaged last-run snapshot")
    m2.metric("Silver articles", f"{silver.get('articles',0):,}")
    m3.metric("Quarantined", f"{silver.get('quarantined',0):,}")
    m4.metric("Gold issue events", f"{gold.get('issue_events',0):,}")
    left, right = st.columns([1.1, 1])
    with left:
        st.markdown("#### How volume changes across layers")
        layer_frame = pd.DataFrame({"Rows": [bronze.get("total",0), silver.get("articles",0), gold.get("issue_events",0)]}, index=["Bronze · all feed records", "Silver · normalized articles", "Gold · classified issue events"])
        st.bar_chart(layer_frame, color="#5636ad", horizontal=True)
        st.caption("These counts describe different grains: source rows, deduplicated articles, then classified issue events.")
    with right:
        st.markdown("#### Gold issue mix · live Supabase rows loaded")
        if not counts.empty:
            issue_mix = counts.groupby("issue_type", dropna=False)["n_reports"].sum().sort_values(ascending=False).to_frame("Observed reports")
            st.bar_chart(issue_mix, color="#087b86", horizontal=True)
            st.caption("Recent loaded rows only; counts reflect news reports, not confirmed incidents.")
        else:
            st.info("Live Gold counts are unavailable right now.")
        st.markdown(f"**Weather feature snapshot:** {weather_stats.get('rows',0):,} hourly rows across {weather_stats.get('cities',0)} city: {', '.join(weather_stats.get('city_names',[])) or 'not recorded'}. This coverage is limited and should not be read as national coverage.")
    st.markdown("#### One normalized Silver record")
    st.caption("Illustrates what changes from a raw source record: standardized fields, inferred city/issue labels, imputation flags, and verification status.")
    silver_example = snap.get("sample_silver_record")
    if silver_example:
        st.code(_records_json(silver_example), language="json")
    dqa = snap.get("dq_actions_latest_run", {})
    if dqa:
        st.markdown("#### Last local run · data-quality actions")
        dq_cols = st.columns(4)
        for col, action in zip(dq_cols, ("pass", "fix", "fill", "flag")):
            col.metric(action.title(), f"{dqa.get(action,0):,}")
        st.caption("Action counts are checks/affected rows and may overlap. Zero quarantined rows does not mean every value is verified.")

# 04 — model, evaluation and current forecast
if selected_stage == NAV_ITEMS[3]:
    st.markdown('<div class="stage-note" style="border-color:var(--teal)"><h3>04 · Model and evaluate</h3><p>The model estimates future counts of classified news reports for a city and issue. It does not predict confirmed emergencies.</p></div>', unsafe_allow_html=True)
    st.markdown("#### Why Poisson regression?")
    st.write("Poisson regression is an interpretable starting point for non-negative count outcomes. CivicPulse uses city, issue type, hour/day, and available weather features. The predicted mean and a 95% Poisson interval are useful for inspecting estimates, but sparse data makes small local forecasts uncertain.")
    if not forecasts.empty:
        city_options = sorted(forecasts["city"].dropna().unique().tolist())
        issue_options = sorted(forecasts["issue_type"].dropna().unique().tolist())
        csel, isel = st.columns(2)
        chosen_city = csel.selectbox("Forecast location", city_options, key="model_city") if city_options else None
        chosen_issue = isel.selectbox("Issue group", issue_options, key="model_issue") if issue_options else None
        selected_forecasts = forecasts.copy()
        if chosen_city: selected_forecasts = selected_forecasts[selected_forecasts.city == chosen_city]
        if chosen_issue: selected_forecasts = selected_forecasts[selected_forecasts.issue_type == chosen_issue]
        latest_row = selected_forecasts.sort_values("forecast_for_utc").iloc[0] if not selected_forecasts.empty else None
    else:
        selected_forecasts, latest_row = forecasts, None
    if latest_row is not None:
        a,b,c,d=st.columns(4)
        a.metric("Expected report count", f"{latest_row.expected_reports:.6f}")
        b.metric("95% lower bound", f"{int(latest_row.lower_95)}")
        c.metric("95% upper bound", f"{int(latest_row.upper_95)}")
        d.metric("Training rows", f"{int(latest_row.training_rows):,}")
        st.caption(f"Selected forecast hour: {latest_row.forecast_for_utc.strftime('%Y-%m-%d %H:%M UTC')} · generated {latest_row.generated_at_utc.strftime('%Y-%m-%d %H:%M UTC')} · expected value kept at full precision.")
        chart_data = selected_forecasts.sort_values("forecast_for_utc").set_index("forecast_for_utc")[["expected_reports"]]
        st.markdown("#### Next-hour estimates in the newest saved run")
        st.line_chart(chart_data, color="#087b86")
        sample_forecast={k:(v.isoformat() if hasattr(v,"isoformat") else v) for k,v in latest_row.to_dict().items()}
        with st.expander("View one saved forecast record as JSON"):
            st.code(_records_json(sample_forecast),language="json")
    else:
        st.info("No matching saved forecast rows are available from Supabase yet.")
    st.markdown("#### Locked historical evaluation")
    eval_data = pd.DataFrame({"MAE": [0.0000551752, 0.0000690631]}, index=["Weekly seasonal baseline", "Poisson regression"])
    st.bar_chart(eval_data, color="#2463d3", horizontal=True)
    e1,e2,e3=st.columns(3)
    e1.metric("Chronological test grid", "996,824 rows")
    e2.metric("Actual reports in test", "28")
    e3.metric("Better full-grid MAE", "Weekly baseline")
    st.markdown('<div class="callout"><b>How to read the evaluation:</b> most test grid cells had zero reports, so the small full-grid MAE is dominated by zeros. In the recorded comparison, the weekly baseline scored better than Poisson. Forecasts are experimental report-count estimates, not calibrated risk probabilities.</div>', unsafe_allow_html=True)
    if not forecasts.empty:
        st.markdown("#### Recent observed Gold counts from Supabase")
        if not counts.empty:
            timeline = counts.groupby("hour_utc")["n_reports"].sum().sort_index().to_frame("Observed news reports")
            st.area_chart(timeline, color="#2463d3")
            st.dataframe(counts.head(12), use_container_width=True, hide_index=True)
    st.caption("Location labels are inherited from source text/geography and can include Unknown, neighborhoods, or non-city places. Coverage is uneven.")

# 05 — RAG knowledge
if selected_stage == NAV_ITEMS[4]:
    st.markdown('<div class="stage-note" style="border-color:var(--orange)"><h3>05 · Retrieval-augmented generation knowledge</h3><p>RAG retrieves matching reference passages and structured CivicPulse results, then provides them as evidence to the answer layer.</p></div>', unsafe_allow_html=True)
    st.markdown('<div class="step-flow"><div class="step"><b>1 · Collect</b><strong>Small trusted set</strong><span>Selected public-service pages, two supplied PDFs, and CivicPulse model/data documentation.</span></div><div class="step"><b>2 · Prepare</b><strong>Chunk + describe</strong><span>Text is split into passages and tagged with publisher, source link, location, and chunk ID.</span></div><div class="step"><b>3 · Index</b><strong>Embed locally</strong><span>A multilingual sentence embedding model stores vectors and metadata in Chroma.</span></div><div class="step"><b>4 · Retrieve</b><strong>Bring evidence</strong><span>Question matches are combined with exact Supabase counts/forecasts when city and issue are named.</span></div></div>', unsafe_allow_html=True)
    try:
        chunk_count = get_rag_chunk_count()
        st.metric("Packaged document chunks", f"{chunk_count:,}")
    except Exception:
        st.metric("Packaged document chunks", "Index unavailable")
    st.markdown("#### Why these resources?")
    st.write("Official PMD, KW&SC, and K-Electric pages help answer service and safety-guidance questions. NDMA/PCP PDFs cover emergency preparedness and complaint procedures. The CivicPulse model card and source notes help the assistant explain what the data and model can—and cannot—say.")
    rag_refs=[("PMD","https://weather.gov.pk/"),("KW&SC","https://www.kwsc.gos.pk/"),("K-Electric","https://ke.com.pk/"),("Citizen Portal","https://citizenportal.gov.pk/"),("NDMA","https://ndma.gov.pk/")]
    for label,url in rag_refs:
        st.markdown(_external_link(label,url),unsafe_allow_html=True)
    st.markdown("#### How knowledge connects to CivicPulse data")
    st.write("For general guidance questions, retrieval uses reference passages. For a recognized city or issue, the app also reads observed hourly counts from Supabase; when both are named, it adds the newest saved Poisson forecast. Structured values come from Gold rows, not from the language model's memory.")
    st.markdown('<div class="callout"><b>Coverage limit:</b> the document index is a curated reference set, not a full web search. A source can be unavailable or outdated; answers should link back to the original source. The historical NDMA PDF is labelled as historical guidance, not a current warning.</div>',unsafe_allow_html=True)

# 06 — GenAI
if selected_stage == NAV_ITEMS[5]:
    st.markdown('<div class="stage-note" style="border-color:var(--pink)"><h3>06 · Generative answer layer</h3><p>OpenRouter provides an OpenAI-compatible chat API. The model writes a concise answer from retrieved evidence and attaches source labels.</p></div>', unsafe_allow_html=True)
    st.markdown('<div class="step-flow"><div class="step"><b>Question</b><strong>User asks</strong><span>The question is used to retrieve relevant chunks and matching structured rows.</span></div><div class="step"><b>Evidence</b><strong>Context is assembled</strong><span>RAG supplies snippets, source URLs, and row/run IDs to the language model.</span></div><div class="step"><b>OpenRouter</b><strong>Answer is drafted</strong><span>A configured LLM receives the question and evidence; it is not trained on CivicPulse data.</span></div><div class="step"><b>Grounding</b><strong>Citations are shown</strong><span>Unsupported questions use the fixed fallback response.</span></div></div>', unsafe_allow_html=True)
    left,right=st.columns([1.2,1])
    with left:
        st.markdown("#### What the AI does")
        st.write("The embedding model finds relevant passages. OpenRouter's selected chat model turns retrieved context into readable wording. The chat model does not compute Poisson forecasts and should not invent counts, event confirmations, or current official warnings.")
        st.markdown("#### Guardrails shown in the app")
        st.markdown("- Answers are limited to retrieved evidence.\n- Every factual claim should have a source marker such as `[S1]`.\n- Observed reports, forecasts, and confirmed incidents are described separately.\n- Evidence without adequate support returns the fallback response.")
    with right:
        model_name=_secret("OPENROUTER_MODEL","openai/gpt-4o-mini")
        st.markdown(f'<div class="dark-callout"><strong>Answer path</strong><br>Question → local Chroma retrieval → Supabase Gold evidence → OpenRouter ({model_name}) → cited response</div>',unsafe_allow_html=True)
        st.markdown("#### What is sent to OpenRouter")
        st.write("The submitted question and selected evidence snippets are sent to the configured OpenRouter model to produce an answer. Do not enter personal or sensitive information in the chat.")

# 07 — cited chat
if selected_stage == NAV_ITEMS[6]:
    st.markdown('<div class="stage-note" style="border-color:var(--blue)"><h3>07 · Ask CivicPulse</h3><p>Ask a question about official guidance, the project’s data/model limits, or a named city and issue. Answers show references and structured row IDs.</p></div>', unsafe_allow_html=True)
    st.markdown('<div class="callout">Try: <b>“What is the latest Poisson estimate for Water in Karachi?”</b> · <b>“What does a report count mean?”</b> · <b>“What should someone check during a heavy-rain warning?”</b></div>',unsafe_allow_html=True)
    st.markdown('<div class="dark-callout"><strong>Delivery path</strong><br>Hourly GitHub Actions pipeline → Supabase Gold + forecast tables → this Streamlit app reads the latest rows. The document index is packaged with the app; chatbot answers retrieve that knowledge and matching Supabase evidence.</div>',unsafe_allow_html=True)
    if "civicpulse_chat" not in st.session_state:
        st.session_state.civicpulse_chat=[]
    for message in st.session_state.civicpulse_chat:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    prompt=st.chat_input("Ask CivicPulse — answers use retrieved sources and cite them")
    if prompt:
        st.session_state.civicpulse_chat.append({"role":"user","content":prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            try:
                with st.spinner("Searching the knowledge base and Supabase evidence…"):
                    evidence=retrieve_app_evidence(prompt)
                    response=generator.answer_from_evidence(
                        prompt,evidence,
                        api_key=_secret("OPENROUTER_API_KEY"),
                        model=_secret("OPENROUTER_MODEL","openai/gpt-4o-mini"),
                        base_url=_secret("OPENROUTER_BASE_URL","https://openrouter.ai/api/v1"),
                    )
                st.markdown(response)
            except Exception as exc:
                response=f"I couldn't complete that answer: {exc}"
                st.error(response)
        st.session_state.civicpulse_chat.append({"role":"assistant","content":response})
    if st.session_state.civicpulse_chat and st.button("Clear this chat", key="clear_civicpulse_chat"):
        st.session_state.civicpulse_chat=[]
        st.rerun()

st.markdown("---")
st.caption(f"CivicPulse · data reads from Supabase · app rendered {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · All displayed event/count times are UTC unless labelled.")
