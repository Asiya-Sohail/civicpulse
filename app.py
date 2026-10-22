"""CivicPulse Streamlit dashboard: Supabase Gold data plus cited RAG answers."""
from __future__ import annotations

import os
import json
import re
import calendar
import html
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

from civicpulse import config
from civicpulse.rag import generator
from civicpulse.rag.index import open_collection
from civicpulse.rag.retriever import _find_city_and_issue

st.set_page_config(page_title="CivicPulse", page_icon="📍", layout="wide")

ISSUE_KEYWORDS = {
    "Electricity": ("electricity", "power outage", "load-shedding", "load shedding", "blackout"),
    "Water": ("water", "waster", "kwsc", "kwsb", "wasa"),
    "Mobility": ("mobility", "traffic", "road closure", "congestion", "commute"),
    "Municipal": ("municipal", "garbage", "waste", "sewerage", "sanitation", "drainage"),
}
ISSUE_REFERENCE = {
    "Electricity": ("K-Electric", "https://www.ke.com.pk/"),
    "Water": ("Karachi Water & Sewerage Corporation", "https://www.kwsc.gos.pk/"),
    "Mobility": ("Pakistan Citizen Portal", "https://citizenportal.gov.pk/"),
    "Municipal": ("Pakistan Citizen Portal", "https://citizenportal.gov.pk/"),
}


def _secret(name: str, default: str = "") -> str:
    """Read a setting from Streamlit secrets first, then environment variables."""
    try:
        value = st.secrets.get(name, default)
    except FileNotFoundError:
        value = default
    return str(value or os.getenv(name, default)).strip()


def _format_dual_time(value, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Render a timestamp in UTC and Pakistan Standard Time (UTC+05:00)."""
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return str(value)
    utc_label = timestamp.strftime(fmt)
    pkt_label = timestamp.tz_convert("Asia/Karachi").strftime(fmt)
    return f"{utc_label} UTC ({pkt_label} PKT)"


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
                  filters: dict[str, str] | None = None, offset: int = 0) -> list[dict]:
    """Read a bounded page from Supabase PostgREST using the app's read-only key."""
    url, key = _supabase_settings()
    params: dict[str, str | int] = {
        "select": select, "order": order, "limit": limit, "offset": offset,
    }
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


def _supabase_get_all(table: str, *, select: str, order: str,
                      filters: dict[str, str] | None = None) -> list[dict]:
    """Page through a bounded time range so longer periods are not silently truncated."""
    rows: list[dict] = []
    page_size = 1000
    for offset in range(0, 500_000, page_size):
        page = _supabase_get(
            table, select=select, order=order, limit=page_size,
            filters=filters, offset=offset,
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows
    raise RuntimeError(f"Supabase time-range query for {table} exceeded 500,000 rows.")


def _detect_issue_type(question: str) -> str | None:
    """Recognize the four supported issue groups, including bare group names."""
    lowered = question.casefold()
    for issue, terms in ISSUE_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in terms):
            return issue
    for issue, patterns in config.ISSUE_PATTERNS.items():
        if any(re.search(pattern, question, re.IGNORECASE) for pattern in patterns):
            return issue
    return None


def _is_city_ranking_question(question: str) -> bool:
    """Detect comparisons that need a structured, cross-city Gold query."""
    return bool(re.search(
        r"\b(which city|what city|most|highest|top|rank|ranking|largest|more than)\b",
        question,
        re.IGNORECASE,
    ))


def _ranking_period(question: str, now_utc: datetime) -> dict[str, object]:
    """Return the requested calendar period; no qualifier means all available history."""
    pkt_now = pd.Timestamp(now_utc).tz_convert("Asia/Karachi")
    today = pkt_now.date()
    lowered = question.casefold()

    def month_number(name: str) -> int | None:
        normalized = name.rstrip(".").lower().replace("sept", "sep")
        return next(
            (month for month in range(1, 13)
             if normalized in {calendar.month_name[month].lower(), calendar.month_abbr[month].lower()}),
            None,
        )

    def day_range(day: date, label: str) -> dict[str, object]:
        start_pkt = pd.Timestamp(day).tz_localize("Asia/Karachi")
        end_pkt = start_pkt + pd.Timedelta(days=1)
        return {
            "kind": "range",
            "granularity": "day",
            "start_date": day.isoformat(),
            "end_date": (day + timedelta(days=1)).isoformat(),
            "start": start_pkt.tz_convert("UTC").to_pydatetime(),
            "end": end_pkt.tz_convert("UTC").to_pydatetime(),
            "label": label,
        }

    if re.search(r"\b(today|this day)\b", lowered):
        return day_range(today, f"{today:%d %b %Y} (Pakistan time)")
    if re.search(r"\byesterday\b", lowered):
        yesterday = today - timedelta(days=1)
        return day_range(yesterday, f"{yesterday:%d %b %Y} (Pakistan time)")

    # An explicitly named weekday means its most recent occurrence in Pakistan time.
    weekdays = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    weekday_match = re.search(
        r"\b(?:on|for|during|from)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lowered,
    )
    if weekday_match:
        target = weekdays[weekday_match.group(1)]
        day = today - timedelta(days=(today.weekday() - target) % 7)
        return day_range(day, f"{day:%d %b %Y} ({weekday_match.group(1).title()}, Pakistan time)")

    # Parse unambiguous numeric dates such as 2026-10-21 or 21/10/2026.
    iso_match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", question)
    dmy_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", question)
    parsed_day = None
    if iso_match:
        try:
            parsed_day = date(int(iso_match[1]), int(iso_match[2]), int(iso_match[3]))
        except ValueError:
            pass
    elif dmy_match:
        try:
            parsed_day = date(int(dmy_match[3]), int(dmy_match[2]), int(dmy_match[1]))
        except ValueError:
            pass
    else:
        month_names = (
            "january|february|march|april|may|june|july|august|september|october|"
            "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
        )
        month_first = re.search(
            rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b",
            lowered,
        )
        day_first = re.search(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})\.?(?:,?\s+(20\d{{2}}))?\b",
            lowered,
        )
        for match, month_group, day_group, year_group in (
            (month_first, 1, 2, 3), (day_first, 2, 1, 3),
        ):
            if match:
                try:
                    month = month_number(match.group(month_group))
                    if month is None:
                        raise ValueError("Unrecognized month")
                    year = int(match.group(year_group)) if match.group(year_group) else today.year
                    parsed_day = date(year, month, int(match.group(day_group)))
                except (ValueError, TypeError):
                    parsed_day = None
                break
    if parsed_day:
        return day_range(parsed_day, f"{parsed_day:%d %b %Y} (Pakistan time)")

    # Explicit year or month requests use that calendar period.
    year_match = re.search(r"\b(20\d{2})\b", question)
    if year_match:
        year = int(year_match.group(1))
        month_names = (
            "january|february|march|april|may|june|july|august|september|october|"
            "november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
        )
        month_match = re.search(rf"\b({month_names})\b", lowered)
        if month_match:
            month = month_number(month_match.group(1))
            if month is not None:
                start_date = date(year, month, 1)
                end_date = date(year + (month == 12), (month % 12) + 1, 1)
                start_pkt = pd.Timestamp(start_date).tz_localize("Asia/Karachi")
                end_pkt = pd.Timestamp(end_date).tz_localize("Asia/Karachi")
                return {
                    "kind": "range",
                    "granularity": "day",
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "start": start_pkt.tz_convert("UTC").to_pydatetime(),
                    "end": end_pkt.tz_convert("UTC").to_pydatetime(),
                    "label": f"{start_date:%B %Y} (Pakistan time)",
                }
        start_day = date(year, 1, 1)
        end_day = date(year + 1, 1, 1)
        start_pkt = pd.Timestamp(start_day).tz_localize("Asia/Karachi")
        end_pkt = pd.Timestamp(end_day).tz_localize("Asia/Karachi")
        return {
            "kind": "range",
            "granularity": "day",
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "start": start_pkt.tz_convert("UTC").to_pydatetime(),
            "end": end_pkt.tz_convert("UTC").to_pydatetime(),
            "label": f"calendar year {year} (Pakistan time)",
        }

    if re.search(r"\b(this week|last week)\b", lowered):
        monday = today - timedelta(days=today.weekday())
        start_day = monday if "this week" in lowered else monday - timedelta(days=7)
        end_day = start_day + timedelta(days=7)
        start_pkt = pd.Timestamp(start_day).tz_localize("Asia/Karachi")
        end_pkt = pd.Timestamp(end_day).tz_localize("Asia/Karachi")
        label = "this week" if "this week" in lowered else "last week"
        return {
            "kind": "range",
            "granularity": "day",
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "start": start_pkt.tz_convert("UTC").to_pydatetime(),
            "end": end_pkt.tz_convert("UTC").to_pydatetime(),
            "label": f"{label} ({start_day:%d %b}–{(end_day - timedelta(days=1)):%d %b %Y}, Pakistan time)",
        }

    if re.search(r"\b(this month|last month)\b", lowered):
        first_this_month = today.replace(day=1)
        if "last month" in lowered:
            end_day = first_this_month
            start_day = (first_this_month - timedelta(days=1)).replace(day=1)
            label = f"{start_day:%B %Y} (Pakistan time)"
        else:
            start_day = first_this_month
            end_day = date(today.year + (today.month == 12), (today.month % 12) + 1, 1)
            label = f"this month ({start_day:%B %Y}, Pakistan time)"
        start_pkt = pd.Timestamp(start_day).tz_localize("Asia/Karachi")
        end_pkt = pd.Timestamp(end_day).tz_localize("Asia/Karachi")
        return {
            "kind": "range",
            "granularity": "day",
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "start": start_pkt.tz_convert("UTC").to_pydatetime(),
            "end": end_pkt.tz_convert("UTC").to_pydatetime(),
            "label": label,
        }

    if re.search(
        r"\b(now|right now|at this time|currently|current|recent(?:ly)?|latest|"
        r"last 24 hours|past 24 hours|last hour|past hour)\b",
        lowered,
    ):
        current_hour = now_utc.replace(minute=0, second=0, microsecond=0)
        end = current_hour
        start = end - timedelta(hours=24)
        return {
            "kind": "range", "granularity": "hour", "start": start, "end": end,
            "label": "the latest 24 complete hours",
        }

    return {"kind": "all"}


def _is_guidance_question(question: str) -> bool:
    """Keep service/safety guidance grounded in documents rather than count rows."""
    return bool(re.search(
        r"\b(how (?:do|can|to)|precautions?|safety|what should|report|complaint|"
        r"contact|procedure|guidance|advisory|warning)\b",
        question,
        re.IGNORECASE,
    ))


def _freshness_note(now_utc: datetime) -> str:
    """Describe Gold sync freshness when the optional status table is readable."""
    try:
        status_rows = _supabase_get(
            "data_freshness", select="source,status,checked_utc,last_fetch_utc",
            order="checked_utc.desc", limit=20,
        )
    except Exception:
        return "Gold sync freshness could not be verified."
    checked_values = [
        pd.to_datetime(row.get("checked_utc"), utc=True, errors="coerce")
        for row in status_rows
    ]
    checked_values = [value for value in checked_values if not pd.isna(value)]
    if not checked_values:
        return "Gold sync freshness is not available."
    latest_checked = max(checked_values).to_pydatetime()
    age_hours = (now_utc - latest_checked).total_seconds() / 3600
    checked_label = _format_dual_time(latest_checked, "%d %b %Y %H:%M")
    if age_hours > config.STALE_AFTER_HOURS:
        return f"Warning: the latest Gold sync check was {checked_label}, so this data is stale."
    return f"Gold sync checked at {checked_label}."


def _issue_city_count_answer(question: str) -> str | None:
    """Answer a named city/issue count from the requested period in Gold."""
    if not re.search(r"\b(how many|number of|total (?:reports|count)|count of)\b", question, re.I):
        return None
    city = config.detect_city(question)
    issue = _detect_issue_type(question)
    if city is None or issue is None:
        return None

    now_utc = datetime.now(timezone.utc)
    period = _ranking_period(question, now_utc)
    try:
        if period["kind"] == "all":
            rows = _supabase_get(
                "issue_city_report_totals",
                select="city,issue_type,total_reports,first_report_utc,last_report_utc",
                order="total_reports.desc",
                limit=1000,
                filters={"city": f"eq.{city}", "issue_type": f"eq.{issue}"},
            )
            value_key = "total_reports"
        elif period["granularity"] == "hour":
            rows = _supabase_get(
                "hourly_issue_counts",
                select="row_id,hour_utc,city,issue_type,n_reports",
                order="hour_utc.desc",
                limit=1000,
                filters={
                    "city": f"eq.{city}",
                    "issue_type": f"eq.{issue}",
                    "and": (
                        f"(hour_utc.gte.{period['start'].isoformat()},"
                        f"hour_utc.lt.{period['end'].isoformat()})"
                    ),
                },
            )
            value_key = "n_reports"
        else:
            rows = _supabase_get_all(
                "issue_city_report_daily",
                select="city,issue_type,report_date,total_reports",
                order="report_date.desc",
                filters={
                    "city": f"eq.{city}",
                    "issue_type": f"eq.{issue}",
                    "and": (
                        f"(report_date.gte.{period['start_date']},"
                        f"report_date.lt.{period['end_date']})"
                    ),
                },
            )
            value_key = "total_reports"
    except Exception:
        return (
            f"I couldn't read Gold counts for {city} {issue}. Check the Supabase connection and "
            "read permissions. Ensure both aggregate views from "
            "supabase/issue_city_report_totals.sql have been created."
        )

    if not rows:
        timeframe = "all available history" if period["kind"] == "all" else str(period["label"])
        return (
            f"CivicPulse Gold has no readable {issue} report rows for {city} during {timeframe}. "
            "That means data is unavailable for this query; it does not mean there were no incidents. "
            "Reference: CivicPulse Gold hourly_issue_counts in Supabase."
        )
    total = sum(int(row.get(value_key) or 0) for row in rows)
    timeframe = "across all available Gold history" if period["kind"] == "all" else f"for {period['label']}"
    freshness = _freshness_note(now_utc)
    return (
        f"CivicPulse Gold recorded {total} classified {issue} news reports for {city} "
        f"{timeframe}. {freshness} These are news-report counts, not confirmed incidents. "
        "Reference: CivicPulse Gold hourly_issue_counts in Supabase."
    )


def _issue_city_ranking_answer(question: str) -> str | None:
    """Rank issue counts over all Gold history or the period named in the question."""
    issue = _detect_issue_type(question)
    if issue is None or not _is_city_ranking_question(question):
        return None

    now_utc = datetime.now(timezone.utc)
    period = _ranking_period(question, now_utc)
    use_time_window = period["kind"] != "all"
    use_rolling_24h = period.get("label") == "the latest 24 complete hours"
    current_hour = now_utc.replace(minute=0, second=0, microsecond=0)
    latest_complete_hour = current_hour - timedelta(hours=1)
    window_start = period.get("start") if use_time_window else None
    window_end = period.get("end") if use_time_window else None
    city_filter = "in.(" + ",".join(f'"{city}"' for city in config.KNOWN_CITIES) + ")"
    try:
        if not use_time_window:
            rows = _supabase_get(
                "issue_city_report_totals",
                select="city,issue_type,total_reports,first_report_utc,last_report_utc,observed_hours",
                order="total_reports.desc",
                limit=1000,
                filters={"issue_type": f"eq.{issue}"},
            )
        elif period["granularity"] == "hour":
            rows = _supabase_get(
                "hourly_issue_counts",
                select="row_id,hour_utc,city,issue_type,n_reports",
                order="hour_utc.desc",
                limit=1000,
                filters={
                    "issue_type": f"eq.{issue}",
                    "city": city_filter,
                    "and": (
                        f"(hour_utc.gte.{window_start.isoformat()},"
                        f"hour_utc.lt.{window_end.isoformat()})"
                    ),
                },
            )
        else:
            rows = _supabase_get_all(
                "issue_city_report_daily",
                select="city,issue_type,report_date,total_reports",
                order="report_date.desc",
                filters={
                    "issue_type": f"eq.{issue}",
                    "and": (
                        f"(report_date.gte.{period['start_date']},"
                        f"report_date.lt.{period['end_date']})"
                    ),
                },
            )
    except Exception:
        return (
            f"I couldn't read CivicPulse Gold data for {issue} across cities, so I can't produce "
            "a reliable ranking. Check the Supabase connection and table read permissions. "
            + "Ensure the aggregate views from supabase/issue_city_report_totals.sql exist. "
            + "Reference: CivicPulse Gold in Supabase."
        )

    if not use_time_window:
        city_totals: dict[str, int] = {}
        total_periods: dict[str, tuple[object, object]] = {}
        cities_with_rows: set[str] = set()
        for row in rows:
            city = row.get("city")
            if city not in config.KNOWN_CITIES:
                continue
            try:
                total = int(row.get("total_reports") or 0)
            except (TypeError, ValueError):
                continue
            city_totals[city] = total
            total_periods[city] = (row.get("first_report_utc"), row.get("last_report_utc"))
            cities_with_rows.add(city)

        freshness = _freshness_note(now_utc)
        if not city_totals:
            return (
                f"CivicPulse Gold has no readable all-time {issue} news-report rows for configured "
                f"cities, so it can't rank them. Missing rows do not prove there were no real-world "
                f"incidents. {freshness} Reference: CivicPulse Gold hourly_issue_counts in Supabase."
            )
        highest_total = max(city_totals.values())
        leaders = sorted(city for city, total in city_totals.items() if total == highest_total)
        period_starts = [pd.to_datetime(pair[0], utc=True, errors="coerce") for pair in total_periods.values()]
        period_ends = [pd.to_datetime(pair[1], utc=True, errors="coerce") for pair in total_periods.values()]
        period_starts = [value for value in period_starts if not pd.isna(value)]
        period_ends = [value for value in period_ends if not pd.isna(value)]
        period_label = ""
        if period_starts and period_ends:
            period_label = (
                f" across available Gold history from {_format_dual_time(min(period_starts))} "
                f"to {_format_dual_time(max(period_ends))}"
            )
        return (
            f"Among {len(cities_with_rows)} configured cities with {issue} rows in CivicPulse Gold, "
            f"{' and '.join(leaders)} had the most CivicPulse news reports{period_label} "
            f"({highest_total} reports). {freshness} These are classified news-report counts, "
            "not confirmed incidents. Reference: CivicPulse Gold hourly_issue_counts in Supabase."
        )

    city_totals: dict[str, int] = {}
    latest_hour_counts: dict[str, int] = {}
    cities_with_rows: set[str] = set()
    for row in rows:
        city = row.get("city")
        if city not in config.KNOWN_CITIES:
            continue
        if period["granularity"] == "hour":
            hour = pd.to_datetime(row.get("hour_utc"), utc=True, errors="coerce")
            if pd.isna(hour):
                continue
            hour_utc = hour.to_pydatetime()
            if hour_utc >= window_end:
                continue
            count = int(row.get("n_reports") or 0)
            if hour_utc == latest_complete_hour:
                latest_hour_counts[city] = latest_hour_counts.get(city, 0) + count
        else:
            count = int(row.get("total_reports") or 0)
        city_totals[city] = city_totals.get(city, 0) + count
        cities_with_rows.add(city)

    if use_rolling_24h:
        window_label = (
            f"{_format_dual_time(window_start, '%d %b %Y %H:%M')} to "
            f"{_format_dual_time(window_end, '%d %b %Y %H:%M')}"
        )
    else:
        window_label = str(period["label"])
    latest_hour_label = (
        f"{_format_dual_time(latest_complete_hour, '%d %b %Y %H:%M')} to "
        f"{_format_dual_time(window_end, '%H:%M')}"
    )
    freshness = _freshness_note(now_utc)
    if not city_totals:
        return (
            f"CivicPulse Gold has no readable {issue} news-report rows for {window_label}, "
            "so it can't rank cities; missing rows do not prove there were "
            f"no real-world incidents. {freshness} Reference: CivicPulse Gold hourly_issue_counts in Supabase."
        )

    highest_total = max(city_totals.values())
    leaders = sorted(city for city, total in city_totals.items() if total == highest_total)
    leader_text = " and ".join(leaders)
    observed_city_count = len(cities_with_rows)
    time_description = (
        f"in the latest 24 complete hours ({window_label})"
        if use_rolling_24h
        else f"during {window_label}"
    )
    answer = (
        f"Among {observed_city_count} configured cities with {issue} rows in CivicPulse Gold, "
        f"{leader_text} had the most CivicPulse news reports {time_description} "
        f"({highest_total} reports)."
    )
    if use_rolling_24h and latest_hour_counts:
        hour_max = max(latest_hour_counts.values())
        hour_leaders = sorted(city for city, count in latest_hour_counts.items() if count == hour_max)
        answer += (
            f" In the latest complete hour ({latest_hour_label}), {', '.join(hour_leaders)} "
            f"had the most among cities with rows ({hour_max} reports)."
        )
    elif use_rolling_24h:
        answer += f" No {issue} rows were recorded in the latest complete hour ({latest_hour_label})."
    answer += (
        f" {freshness} These are classified news-report counts, not confirmed incidents. "
        f"Reference: CivicPulse Gold hourly_issue_counts in Supabase."
    )
    return answer


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
def load_recent_gold_news(limit: int = 3) -> pd.DataFrame:
    """Load a few latest individual, classified news reports from Gold issue_events."""
    records = _supabase_get(
        "issue_events",
        select="row_id,title,url,publisher,published_utc,city,issue_type",
        order="published_utc.desc",
        limit=limit,
        filters={"issue_type": "neq.Other", "title": "not.is.null"},
    )
    frame = pd.DataFrame(records)
    if not frame.empty:
        frame["published_utc"] = pd.to_datetime(frame["published_utc"], utc=True, errors="coerce")
        frame = frame.sort_values("published_utc", ascending=False, na_position="last").head(limit)
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


@st.cache_data(ttl=300, show_spinner=False)
def load_recent_source_coverage() -> tuple[pd.DataFrame, str, str]:
    """Load all configured issue rows in the latest 24 complete hours from Supabase."""
    now_utc = datetime.now(timezone.utc)
    current_hour = now_utc.replace(minute=0, second=0, microsecond=0)
    window_end = current_hour
    window_start = window_end - timedelta(hours=24)
    records: list[dict] = []
    page_size = 1000
    for offset in range(0, 10_000, page_size):
        page = _supabase_get(
            "hourly_issue_counts",
            select="row_id,hour_utc,city,issue_type,n_reports",
            order="hour_utc.desc",
            limit=page_size,
            offset=offset,
            filters={
                "and": (
                    f"(hour_utc.gte.{window_start.isoformat()},"
                    f"hour_utc.lt.{window_end.isoformat()})"
                ),
            },
        )
        records.extend(page)
        if len(page) < page_size:
            break
    frame = pd.DataFrame(records)
    if not frame.empty:
        frame["hour_utc"] = pd.to_datetime(frame["hour_utc"], utc=True, errors="coerce")
        frame["n_reports"] = pd.to_numeric(frame["n_reports"], errors="coerce").fillna(0)
        frame = frame[frame["issue_type"].isin(ISSUE_KEYWORDS)].copy()
    return frame, window_start.isoformat(), window_end.isoformat()


def _gold_evidence(question: str) -> list[dict]:
    """Fetch exact observed counts and latest forecast rows from Supabase."""
    city, issue = _find_city_and_issue(question)
    issue = issue or _detect_issue_type(question)
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
                f"Issue: {row['issue_type']}. Hour: {_format_dual_time(hour)}. This is a report count, "
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
            latest_run_rows = [row for row in forecasts if row["generated_at_utc"] == newest]
            normalized_question = question.casefold().replace("-", " ")
            if "next hour" in normalized_question:
                # A next-hour question asks for the nearest forecast point, not
                # the entire 24-hour horizon.
                latest_run_rows = latest_run_rows[:1]
            for row in latest_run_rows:
                row_id = f"{row['forecast_run_id']}_{row['city']}_{row['issue_type']}_{row['forecast_for_utc']}"
                evidence.append({
                    "text": (
                        "CivicPulse Poisson forecast of future hourly news-report counts; "
                        "this is an estimate, not a confirmed incident count. "
                        f"City: {row['city']}. Issue: {row['issue_type']}. "
                        f"Forecast hour: {_format_dual_time(row['forecast_for_utc'])}. "
                        f"Expected reports: {float(row['expected_reports']):.6f}; "
                        f"95% prediction interval: {row['lower_95']} to {row['upper_95']}. "
                        f"Generated at: {_format_dual_time(row['generated_at_utc'])}; training cutoff: "
                        f"{_format_dual_time(row['training_cutoff_utc'])}; training rows: {row['training_rows']}; "
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
    display_record = dict(record)
    for key, value in record.items():
        if key.lower().endswith("_utc") and value:
            display_record[f"{key[:-4]}_pkt"] = _format_dual_time(value)
    return json.dumps(display_record, ensure_ascii=False, indent=2, default=default)


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


def _go_to_chat() -> None:
    """Switch the primary navigation to the chat page from the overview CTA."""
    st.session_state["primary_navigation"] = "Ask CivicPulse"


# Modern, responsive visual system for a simple overview-first experience.
st.markdown("""
<style>
:root { --navy:#101d32; --ink:#1d2a3d; --muted:#68768c; --paper:#f1f4fa; --line:#dce3ee;
        --blue:#2463d3; --green:#168058; --purple:#5636ad; --teal:#087b86; --orange:#b44516; --pink:#ad1457; }
html, body, [class*="css"] { font-family:Inter,"Segoe UI",ui-sans-serif,system-ui,sans-serif; }
[data-testid="stAppViewContainer"] { background:linear-gradient(180deg,#f8faff 0%,#f0f4fa 54%,#f7f9fc 100%); }
[data-testid="stHeader"] { background:transparent; }
.block-container { max-width:1160px; padding:1.15rem clamp(1rem,3vw,2.25rem) 2.25rem; }
.hero { background:radial-gradient(ellipse at 85% 0%,#274e79 0%,transparent 42%),linear-gradient(115deg,#101d32 0%,#172b49 100%); border-radius:24px;
  color:#f8faff; padding:clamp(1.35rem,3vw,2rem); margin:0 0 1.15rem; box-shadow:0 18px 48px #172b4918; border:1px solid #ffffff12; }
.eyebrow { color:#a8bad7; font-size:.7rem; font-weight:750; letter-spacing:.16em; text-transform:uppercase; margin-bottom:.65rem; }
.hero h1 { color:white; font:800 clamp(2rem,5vw,3.1rem)/1.08 Inter,"Segoe UI",sans-serif; letter-spacing:-.045em; margin:0; }
.hero p { color:#c0ccdc; max-width:660px; font-size:.98rem; line-height:1.55; margin:.65rem 0 0; }
.home-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1rem; margin:1rem 0; }
.home-card { background:rgba(255,255,255,.88); border:1px solid var(--line); border-radius:19px; padding:1.25rem; box-shadow:0 10px 30px #1e35510a; height:100%; }
.home-card .kicker { color:#74839a; font-size:.68rem; font-weight:750; letter-spacing:.12em; text-transform:uppercase; }
.home-card h3 { color:var(--ink); font:750 1.12rem Inter,"Segoe UI",sans-serif; margin:.45rem 0 .5rem; }
.home-card p { color:var(--muted); font-size:.92rem; line-height:1.6; margin:0; }
.home-band { background:linear-gradient(110deg,#e8f1ff,#edf7f6); border:1px solid #dce8f4; border-radius:20px; padding:1.2rem 1.35rem; margin:1rem 0; }
.home-band h3 { margin:0 0 .35rem; color:var(--ink); font-size:1.05rem; }
.home-band p { margin:0; color:#596a80; line-height:1.6; }
.news-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.85rem; margin:.85rem 0 1.15rem; }
.news-card { display:flex; flex-direction:column; min-height:190px; background:rgba(255,255,255,.92); border:1px solid var(--line); border-radius:18px; padding:1rem 1.05rem; box-shadow:0 8px 24px #1e35510a; }
.news-meta { color:#247c82; font-size:.68rem; font-weight:750; letter-spacing:.09em; text-transform:uppercase; }
.news-title { color:var(--ink); font:700 1rem/1.45 Inter,"Segoe UI",sans-serif; margin:.55rem 0 .8rem; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
.news-footer { margin-top:auto; display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:.4rem; color:var(--muted); font-size:.76rem; }
.news-link { color:#2860bd; font-weight:700; text-decoration:none; }
.news-link:hover { text-decoration:underline; }
.topic-pills { display:flex; flex-wrap:wrap; gap:.5rem; margin:.85rem 0 1.1rem; }
.topic-pill { color:#27466a; background:#eaf1fb; border:1px solid #d7e3f3; padding:.42rem .72rem; border-radius:999px; font-size:.82rem; font-weight:650; }
.overview-cta { display:flex; align-items:center; justify-content:space-between; gap:1rem; background:#fff; border:1px solid var(--line); border-radius:18px; padding:1rem 1.15rem; box-shadow:0 8px 24px #1e35510a; }
.overview-cta h3 { color:var(--ink); font-size:1rem; margin:0 0 .22rem; }
.overview-cta p { color:var(--muted); font-size:.86rem; line-height:1.45; margin:0; }
.section-heading { color:var(--ink); font:750 1.35rem Inter,"Segoe UI",sans-serif; letter-spacing:-.02em; margin:1.4rem 0 .35rem; }
.section-subtitle { color:var(--muted); line-height:1.6; margin:0 0 .9rem; }
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
section[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] { gap:.42rem; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label { background:#ffffff08; border:1px solid #ffffff0c;
  border-radius:13px; padding:.68rem .78rem; transition:background .18s ease,border .18s ease; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label:hover { background:#ffffff16; border-color:#ffffff30; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) { background:#2463d3; border-color:#73a0fa; }
section[data-testid="stSidebar"] [data-testid="stRadio"] label p { font-weight:700; font-size:.9rem; }
section[data-testid="stSidebar"] [data-testid="stButton"] button { border:1px solid #587094; background:#1d3557; color:white; border-radius:11px; }
section[data-testid="stSidebar"] [data-testid="stButton"] button:hover { background:#28518a; border-color:#9bb8e8; color:white; }
section[data-testid="stSidebar"] [data-testid="stSelectbox"] div[data-baseweb="select"] > div { background:#1d3557; border-color:#587094; color:#fff; }
section[data-testid="stSidebar"] hr { border-color:#ffffff22; }
section[data-testid="stSidebar"] small, section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color:#aebdd1 !important; }
div[data-testid="stMetric"] { background:#fff; border:1px solid var(--line); border-radius:15px; padding:.85rem 1rem; }
div[data-testid="stMetricLabel"] p { color:#69778c; font-weight:600; }
div[data-testid="stMetricValue"] { color:var(--ink); font-family:Inter,"Segoe UI",sans-serif; }
div[data-testid="stChatMessage"] { border:1px solid var(--line); border-radius:18px; box-shadow:0 4px 16px #1e35510a; }
div[data-testid="stChatInput"] { border-radius:16px; box-shadow:0 8px 24px #1e355114; }
@media (max-width:760px) { .home-grid { grid-template-columns:1fr; }
  .news-grid { grid-template-columns:1fr; }
  .step-flow { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .block-container { padding-left:.8rem; padding-right:.8rem; }
  section[data-testid="stSidebar"] { min-width:250px; }
}
@media (max-width:420px) { .hero-grid,.step-flow { grid-template-columns:1fr 1fr; gap:.45rem; }
  .hero { border-radius:18px; padding:1.35rem; } }
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

STAGE_ITEMS = (
    "01  Problem",
    "02  Sources",
    "03  Data pipeline",
    "04  Forecast model",
    "05  RAG knowledge",
    "06  Generative AI",
    "07  Ask CivicPulse",
)
PAGE_ITEMS = ("Overview", "Ask CivicPulse", "Project details")

with st.sidebar:
    st.markdown("<div class='eyebrow'>CIVICPULSE</div>", unsafe_allow_html=True)
    st.markdown("## Civic signals, clearly.")
    st.caption("Public-service reporting for Pakistan")
    selected_page = st.radio("MENU", PAGE_ITEMS, index=0, key="primary_navigation", label_visibility="collapsed")
    if selected_page == "Overview":
        selected_stage = STAGE_ITEMS[0]
    elif selected_page == "Ask CivicPulse":
        selected_stage = STAGE_ITEMS[6]
    else:
        st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)
        selected_stage = st.selectbox(
            "Explore project details",
            STAGE_ITEMS[1:6],
            format_func=lambda item: item.split("  ", 1)[1],
            label_visibility="visible",
        )
    st.markdown("---")
    if st.button("↻ Refresh Supabase data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    if not data_error and not pd.isna(latest_count):
        st.markdown(f"**Data updated through**  \n{_format_dual_time(latest_count, '%d %b %Y · %H:%M')}")
    st.caption("Live counts and forecasts come from Supabase. Refresh clears this app’s cache.")

st.markdown(f"""
<section class="hero">
  <div class="eyebrow">PUBLIC-SERVICE SIGNALS · PAKISTAN</div>
  <h1>{'Ask CivicPulse' if selected_page == 'Ask CivicPulse' else 'See what people are reporting.' if selected_page == 'Overview' else 'Inside the project'}</h1>
  <p>{'Ask about service guidance, report patterns, or model estimates. Answers use retrieved references and available CivicPulse data.' if selected_page == 'Ask CivicPulse' else 'CivicPulse brings news reports about water, electricity, roads, and city services into one easy-to-explore place.' if selected_page == 'Overview' else 'Technical details are here when you need them. Choose a section from the sidebar; the main experience stays focused on the project and its answers.'}</p>
</section>
""", unsafe_allow_html=True)

if data_error:
    st.info("Live data is temporarily unavailable. Project information and the knowledge assistant remain available where configured.")

# 01 — Overview: purpose first, technical detail on request
if selected_page == "Overview":
    st.markdown('<div class="topic-pills"><span class="topic-pill">Water</span><span class="topic-pill">Electricity</span><span class="topic-pill">Roads & travel</span><span class="topic-pill">City services</span></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-heading">Recent news reports</div><p class="section-subtitle">A few of the latest reports classified in CivicPulse Gold. Open the original article for the full story.</p>', unsafe_allow_html=True)
    try:
        recent_news = load_recent_gold_news(3)
        if recent_news.empty:
            st.info("No classified news reports are available in Gold yet.")
        else:
            cards = []
            for _, report in recent_news.iterrows():
                title = html.escape(str(report.get("title") or "Untitled news report"))
                issue = html.escape(str(report.get("issue_type") or "Civic issue"))
                city = html.escape(str(report.get("city") or "Location unavailable"))
                publisher = html.escape(str(report.get("publisher") or "News source"))
                published = report.get("published_utc")
                date_label = "Date unavailable" if pd.isna(published) else html.escape(
                    _format_dual_time(published, "%d %b %Y · %H:%M")
                )
                source_url = str(report.get("url") or "").strip()
                if source_url.startswith(("https://", "http://")):
                    safe_url = html.escape(source_url, quote=True)
                    source_link = f'<a class="news-link" href="{safe_url}" target="_blank" rel="noopener noreferrer">Read source ↗</a>'
                else:
                    source_link = ""
                cards.append(
                    f'<article class="news-card"><div class="news-meta">{issue} <span aria-hidden="true">·</span> {city}</div>'
                    f'<div class="news-title">{title}</div><div class="news-footer">'
                    f'<span>{publisher}<br>{date_label}</span>{source_link}</div></article>'
                )
            st.markdown('<div class="news-grid">' + "".join(cards) + "</div>", unsafe_allow_html=True)
    except Exception:
        st.info("Recent Gold news reports are temporarily unavailable.")
    st.markdown('<div class="section-heading">Why CivicPulse?</div><p class="section-subtitle">Public-service information is spread across news, weather, and official guidance. CivicPulse brings these signals together so people can understand what is being reported and find useful, sourced information.</p>', unsafe_allow_html=True)
    st.markdown('''<div class="home-grid">
      <div class="home-card"><div class="kicker">01 · Bring signals together</div><h3>Reports, place, and time</h3><p>News, weather, and historical event data are organized into four public-service topics and grouped by location and hour.</p></div>
      <div class="home-card"><div class="kicker">02 · Make patterns easier to see</div><h3>Counts and estimates</h3><p>Quality checks prepare the data. The Poisson model estimates future report counts; it does not confirm that an incident will happen.</p></div>
      <div class="home-card"><div class="kicker">03 · Help answer questions</div><h3>Guidance with references</h3><p>The chatbot looks up CivicPulse data or selected documents and official guidance, then gives a concise answer with sources when available.</p></div>
    </div>''', unsafe_allow_html=True)
    st.markdown('<div class="home-band"><h3>Who is it for?</h3><p>Residents can ask where to find service guidance. Planners and evaluators can explore report patterns and model estimates. All counts describe news reports, not verified incidents or official service status.</p></div>', unsafe_allow_html=True)
    cta_text, cta_action = st.columns([3, 1], vertical_alignment="center")
    with cta_text:
        st.markdown('<div class="overview-cta"><div><h3>Have a question?</h3><p>Ask about a service, a place, or a report estimate. Answers include sources when available.</p></div></div>', unsafe_allow_html=True)
    with cta_action:
        st.button("Ask CivicPulse →", type="primary", use_container_width=True, on_click=_go_to_chat)
    st.caption("CivicPulse summarizes news reports. A report is not a confirmed incident. Explore more in Project details.")

# 02 — Sources and bounded raw samples
if selected_stage == STAGE_ITEMS[1]:
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
    st.markdown("#### Recent Gold reporting coverage · live Supabase data")
    try:
        coverage, coverage_start, coverage_end = load_recent_source_coverage()
        st.caption(
            f"Latest 24 complete hours: {_format_dual_time(coverage_start, '%d %b %Y %H:%M')} "
            f"to {_format_dual_time(coverage_end, '%d %b %Y %H:%M')}. "
            "This reflects classified Gold reports, not raw feed volume."
        )
        if coverage.empty:
            st.info("No readable Gold report rows were found in this 24-hour window.")
        else:
            known_city_rows = coverage[coverage["city"].isin(config.KNOWN_CITIES)]
            unmapped_rows = int((~coverage["city"].isin(config.KNOWN_CITIES)).sum())
            report_total = int(coverage["n_reports"].sum())
            city_total = int(known_city_rows["city"].nunique())
            issue_total = int(coverage["issue_type"].nunique())
            latest_observed = coverage["hour_utc"].max()
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Classified news reports", f"{report_total:,}")
            m2.metric("Configured cities with rows", f"{city_total:,}")
            m3.metric("Issue groups represented", f"{issue_total:,} / 4")
            m4.metric("Unmapped location rows", f"{unmapped_rows:,}")

            issue_summary = (
                coverage.groupby("issue_type", as_index=False)
                .agg(
                    news_reports=("n_reports", "sum"),
                    cities_with_rows=("city", lambda values: values[values.isin(config.KNOWN_CITIES)].nunique()),
                    latest_observed_utc=("hour_utc", "max"),
                )
                .sort_values("news_reports", ascending=False)
            )
            issue_summary["latest_observed_utc"] = issue_summary["latest_observed_utc"].map(
                lambda value: _format_dual_time(value)
            )
            issue_summary = issue_summary.rename(columns={
                "latest_observed_utc": "Latest observed hour (UTC / PKT)",
                "issue_type": "Issue group",
                "news_reports": "Classified news reports",
                "cities_with_rows": "Cities with rows",
            })
            st.dataframe(issue_summary, use_container_width=True, hide_index=True)
            st.caption(
                f"Latest observed report hour: {_format_dual_time(latest_observed)}. "
                "Cities without rows are not assumed to have zero reports; counts do not confirm real-world incidents."
            )
    except Exception as exc:
        st.warning(f"Live Gold coverage is unavailable: {exc}")
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
        freshness_display = freshness[[c for c in ("source", "status", "last_fetch_utc", "rows") if c in freshness]].copy()
        if "last_fetch_utc" in freshness_display:
            freshness_display["last_fetch_utc"] = freshness_display["last_fetch_utc"].map(
                lambda value: _format_dual_time(value) if not pd.isna(value) else "Unavailable"
            )
            freshness_display = freshness_display.rename(columns={"last_fetch_utc": "last_fetch_utc (UTC / PKT)"})
        st.dataframe(freshness_display, use_container_width=True, hide_index=True)

# 03 — preprocessing and layers
if selected_stage == STAGE_ITEMS[2]:
    st.markdown('<div class="stage-note" style="border-color:var(--purple)"><h3>03 · Warehouse and data-quality agent</h3><p>Raw records are preserved, cleaned into a shared shape, checked, and grouped into analysis-ready tables. Failed checks can stop a new build so the last trusted Gold output remains available.</p></div>', unsafe_allow_html=True)
    st.markdown('<div class="step-flow"><div class="step"><b>Bronze · raw</b><strong>Keep the original</strong><span>Append source payloads and retain file hashes and manifest provenance for traceability.</span></div><div class="step"><b>Gate 1 → Silver</b><strong>Clean and standardize</strong><span>Normalize text, URLs, aliases, timestamps, and fields; deduplicate and mark imputations.</span></div><div class="step"><b>Gate 2 → Gold</b><strong>Group for analysis</strong><span>Aggregate issue reports by UTC hour, city, and issue; join weather features where available.</span></div><div class="step"><b>Evidence</b><strong>Keep the trail</strong><span>Quality actions, row IDs, source manifests, and file paths explain where data came from.</span></div></div>', unsafe_allow_html=True)
    snap = artifacts
    bronze = snap.get("bronze", {})
    silver = snap.get("silver", {})
    gold = snap.get("gold", {})
    weather_stats = snap.get("weather", {})
    st.caption(f"Layer detail snapshot: last successful local build at {_format_dual_time(snap.get('finished_utc'))} · run `{snap.get('run_id','unknown')}` · {snap.get('status','unknown')}. The deployment does not receive Bronze/Silver rows; its live Gold counts come from Supabase.")
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
if selected_stage == STAGE_ITEMS[3]:
    st.markdown('<div class="stage-note" style="border-color:var(--teal)"><h3>04 · Model and evaluate</h3><p>See the latest saved estimate, the evidence behind the model’s evaluation, and where location data needs review.</p></div>', unsafe_allow_html=True)
    st.markdown("#### What the model estimates")
    st.write("CivicPulse’s Poisson regression estimates hourly classified-news report counts by location and issue group, using Gold counts, calendar features, and available weather. These are report-count estimates, not confirmed incidents or official service status.")

    st.markdown("#### Latest forecast batch · Supabase")
    if not forecasts.empty:
        run = forecasts.sort_values(["forecast_for_utc", "city", "issue_type"])
        configured = run[run["city"].isin(config.KNOWN_CITIES)].copy()
        generated_at = run["generated_at_utc"].max()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Forecast rows", f"{len(run):,}")
        m2.metric("Locations covered", f"{run['city'].nunique():,}")
        m3.metric("Issue groups", f"{run['issue_type'].nunique():,}")
        m4.metric("Training rows", f"{int(run['training_rows'].max()):,}")
        st.caption(f"Run generated {_format_dual_time(generated_at)} · forecast times are shown in Pakistan time (PKT) with UTC retained for traceability.")

        if not configured.empty:
            st.markdown("**Representative forecasts for configured cities**")
            display = configured.head(20).copy()
            display["Forecast time (PKT)"] = display["forecast_for_utc"].map(
                lambda value: _format_dual_time(value, "%Y-%m-%d %H:%M").split("(", 1)[-1].rstrip(")")
            )
            display["Expected reports"] = display["expected_reports"].map(lambda value: f"{value:.6f}")
            display["95% interval"] = display.apply(lambda row: f"{int(row.lower_95)}–{int(row.upper_95)}", axis=1)
            st.dataframe(
                display[["Forecast time (PKT)", "city", "issue_type", "Expected reports", "95% interval"]]
                .rename(columns={"city": "Location", "issue_type": "Issue group"}),
                use_container_width=True, hide_index=True,
            )
            st.caption("Showing the first 20 rows ordered by forecast time, location, and issue. Expected reports may be fractional because they are model means.")
    else:
        st.info("Supabase has no saved forecast batch to display yet. The model evaluation below is the recorded historical holdout comparison.")

    st.markdown("#### How well did the model do on past data?")
    st.write("This chart compares the model with a simple weekly-pattern estimate on a past period that was held back from training. Shorter bars mean the estimates were closer to the recorded report counts.")
    eval_data = pd.DataFrame({"Average error in reports per city, issue, and hour": [0.0000551752, 0.0000690631]}, index=["Weekly pattern", "CivicPulse Poisson model"])
    st.bar_chart(eval_data, color="#2463d3", horizontal=True)
    e1,e2,e3 = st.columns(3)
    e1.metric("Past city/issue/hour records checked", "996,824")
    e2.metric("Records with reports", "28")
    e3.metric("Closer in this comparison", "Weekly pattern")
    st.caption("Most records in the past test period had no reports, which makes the average error look very small. The weekly pattern was slightly more accurate than Poisson in this comparison, so treat CivicPulse forecasts as an early estimate while more representative data is collected.")

    st.markdown("#### Reports recorded in recent data")
    st.write("This chart shows classified news reports already recorded in CivicPulse’s processed data, grouped by hour. It is historical observation data—not a forecast and not a count of verified real-world incidents.")
    if not counts.empty:
        timeline = counts.groupby("hour_utc")["n_reports"].sum().sort_index().to_frame("Observed news reports")
        timeline.index = [_format_dual_time(value, "%m-%d %H:%M") for value in timeline.index]
        st.area_chart(timeline, color="#2463d3")
        recent_count_rows = counts.head(12).copy()
        recent_count_rows["hour_pkt"] = recent_count_rows["hour_utc"].map(
            lambda value: _format_dual_time(value).split("(", 1)[-1].rstrip(")")
        )
        recent_count_rows = recent_count_rows.rename(columns={"hour_utc": "hour_utc (UTC)"})
        st.dataframe(recent_count_rows, use_container_width=True, hide_index=True)
    else:
        st.caption("No recent observed Gold count rows are currently available from Supabase.")

# 05 — RAG knowledge
if selected_stage == STAGE_ITEMS[4]:
    st.markdown('<div class="stage-note" style="border-color:var(--orange)"><h3>05 · Retrieval-augmented generation knowledge</h3><p>RAG retrieves matching reference passages and structured CivicPulse results, then provides them as evidence to the answer layer.</p></div>', unsafe_allow_html=True)
    st.markdown('<div class="step-flow"><div class="step"><b>1 · Collect</b><strong>Small trusted set</strong><span>Selected public-service pages, two supplied PDFs, and CivicPulse model/data documentation.</span></div><div class="step"><b>2 · Prepare</b><strong>Chunk + describe</strong><span>Text is split into passages and tagged with publisher, source link, location, and chunk ID.</span></div><div class="step"><b>3 · Index</b><strong>Embed locally</strong><span>A multilingual sentence embedding model stores vectors and metadata in Chroma.</span></div><div class="step"><b>4 · Retrieve</b><strong>Bring evidence</strong><span>Question matches are combined with exact Supabase counts/forecasts when city and issue are named.</span></div></div>', unsafe_allow_html=True)
    try:
        chunk_count = get_rag_chunk_count()
        st.metric("Packaged document chunks", f"{chunk_count:,}")
    except Exception:
        st.metric("Packaged document chunks", "Index unavailable")
    st.markdown("#### How knowledge connects to CivicPulse data")
    st.write("For general guidance questions, retrieval uses reference passages. For a recognized city or issue, the app also reads observed hourly counts from Supabase; when both are named, it adds the newest saved Poisson forecast. Structured values come from Gold rows, not from the language model's memory.")
    st.markdown('<div class="callout"><b>Coverage limit:</b> the document index is a curated reference set, not a full web search. A source can be unavailable or outdated; answers should link back to the original source. The historical NDMA PDF is labelled as historical guidance, not a current warning.</div>',unsafe_allow_html=True)

# 06 — GenAI
if selected_stage == STAGE_ITEMS[5]:
    st.markdown('<div class="stage-note" style="border-color:var(--pink)"><h3>06 · Generative answer layer</h3><p>OpenRouter turns retrieved evidence into concise prose. Gold-data rankings are calculated directly before the answer is written.</p></div>', unsafe_allow_html=True)
    st.markdown('<div class="step-flow"><div class="step"><b>Question</b><strong>Route by intent</strong><span>Guidance questions search documents; city comparisons query Gold tables.</span></div><div class="step"><b>Evidence</b><strong>Context is assembled</strong><span>RAG supplies source passages; structured queries supply exact counts and timestamps.</span></div><div class="step"><b>OpenRouter</b><strong>Answer is drafted</strong><span>The LLM explains retrieved evidence; it does not calculate rankings or forecasts.</span></div><div class="step"><b>Grounding</b><strong>References are shown</strong><span>Missing topic evidence gets a clear unavailable message and an official link.</span></div></div>', unsafe_allow_html=True)
    left,right=st.columns([1.2,1])
    with left:
        st.markdown("#### What the AI does")
        st.write("The embedding model finds relevant passages. OpenRouter's selected chat model turns retrieved context into readable wording. The chat model does not compute Poisson forecasts and should not invent counts, event confirmations, or current official warnings.")
        st.markdown("#### Guardrails shown in the app")
        st.markdown("- City rankings are calculated from Supabase Gold report counts; OpenRouter explains the result but does not calculate it.\n- Rankings compare configured cities with available rows over the latest 24 complete hours and show the latest complete hour plus sync freshness when available.\n- Missing data is described as unavailable; it is not treated as proof of zero incidents.\n- Questions about Electricity, Water, Mobility, or Municipal services get a topic-specific evidence or access message with a relevant official link when CivicPulse lacks support.\n- Guidance answers use retrieved documents, cite only sources used, and appear as one concise paragraph.\n- Observed news counts and Poisson forecast estimates are labelled separately; neither is presented as a confirmed incident or official service status.\n- Timestamps are displayed in UTC and Pakistan Standard Time (PKT).")
    with right:
        model_name=_secret("OPENROUTER_MODEL","openai/gpt-4o-mini")
        st.markdown(f'<div class="dark-callout"><strong>Answer path</strong><br>Guidance: Chroma → OpenRouter ({model_name}) → cited paragraph<br>City ranking: Supabase Gold → deterministic comparison → cited paragraph</div>',unsafe_allow_html=True)
        st.markdown("#### What is sent to OpenRouter")
        st.write("The submitted question and selected evidence snippets are sent to the configured OpenRouter model to produce an answer. Do not enter personal or sensitive information in the chat.")

# 07 — cited chat
if selected_stage == STAGE_ITEMS[6]:
    st.markdown('<div class="section-heading">What would you like to know?</div><p class="section-subtitle">Ask about public-service guidance, recent report patterns, or a CivicPulse forecast. Answers use available evidence and include references when possible.</p>', unsafe_allow_html=True)
    st.markdown('<div class="callout"><b>Try asking:</b> Which city has the most Water reports? · What is the next-hour estimate for Water in Karachi? · What precautions are recommended during a heavy-rain warning?</div>',unsafe_allow_html=True)
    st.caption("Answers draw on CivicPulse’s reference material and saved data when relevant. Sources are shown with the response when available.")
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
                    response = _issue_city_count_answer(prompt)
                    if response is None:
                        response = _issue_city_ranking_answer(prompt)
                    if response is None:
                        issue = _detect_issue_type(prompt)
                        evidence = retrieve_app_evidence(prompt)
                        if _is_guidance_question(prompt):
                            evidence = [
                                item for item in evidence
                                if item["metadata"].get("evidence_type") in {"document", "project_document"}
                            ]
                        response = generator.answer_from_evidence(
                            prompt,evidence,
                            api_key=_secret("OPENROUTER_API_KEY"),
                            model=_secret("OPENROUTER_MODEL","openai/gpt-4o-mini"),
                            base_url=_secret("OPENROUTER_BASE_URL","https://openrouter.ai/api/v1"),
                        )
                        if response == generator.FALLBACK and issue:
                            source_name, source_url = ISSUE_REFERENCE[issue]
                            response = (
                                f"The indexed CivicPulse references and available Gold data do not "
                                f"contain enough evidence to answer this {issue} question. For current "
                                f"official information, check [{source_name}]({source_url})."
                            )
                st.markdown(response)
            except Exception as exc:
                issue = _detect_issue_type(prompt)
                if issue:
                    source_name, source_url = ISSUE_REFERENCE[issue]
                    response = (
                        f"I couldn't access CivicPulse's indexed references or supporting data for "
                        f"this {issue} question right now. For current official information, check "
                        f"[{source_name}]({source_url})."
                    )
                    st.info(response)
                else:
                    response=f"I couldn't complete that answer: {exc}"
                    st.error(response)
        st.session_state.civicpulse_chat.append({"role":"assistant","content":response})
    if st.session_state.civicpulse_chat and st.button("Clear this chat", key="clear_civicpulse_chat"):
        st.session_state.civicpulse_chat=[]
        st.rerun()

st.markdown("---")
st.caption(f"CivicPulse · data reads from Supabase · app rendered {_format_dual_time(datetime.now(timezone.utc), '%Y-%m-%d %H:%M')} · all event/count times show UTC and Pakistan Standard Time (PKT, UTC+05:00).")
