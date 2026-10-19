"""Gold: analytics-ready tables. Every row carries a deterministic, immutable row_id."""
from __future__ import annotations

import duckdb

PKT = "INTERVAL 5 HOUR"  # Pakistan has no DST; used for *display* columns only


def build_gold(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    # One row per reported issue. row_id = hash of the article URL, so it is identical on every rebuild.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE gold.issue_events AS
        SELECT 'r_' || article_id AS row_id, source, publisher_domain, title, url,
               published_utc, published_utc + {PKT} AS published_pkt_display,
               city, province, region_group, lat, lon, issue_type, service,
               urgency_score, is_imputed, imputed_fields, verification_status, dq_flags
        FROM silver.articles
        WHERE issue_type <> 'Other' AND article_id IS NOT NULL
        """
    )
    # Hour x place x issue counts: the table the forecasting model will train on.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE gold.hourly_issue_counts AS
        SELECT 'h_' || substr(md5(CAST(hour_utc AS VARCHAR) || '|' || city || '|' || issue_type), 1, 16) AS row_id,
               hour_utc, hour_utc + {PKT} AS hour_pkt_display, region_group, city, issue_type,
               n_reports, mean_urgency, max_urgency, n_imputed, n_sources
        FROM (
            SELECT date_trunc('hour', published_utc) AS hour_utc, region_group,
                   coalesce(city, 'Unknown') AS city, issue_type,
                   count(*) AS n_reports, avg(urgency_score) AS mean_urgency,
                   max(urgency_score) AS max_urgency,
                   count(*) FILTER (WHERE is_imputed) AS n_imputed,
                   count(DISTINCT source) AS n_sources
            FROM gold.issue_events
            GROUP BY 1, 2, 3, 4
        )
        """
    )
    return {
        "gold_events": con.execute("SELECT count(*) FROM gold.issue_events").fetchone()[0],
        "gold_hourly": con.execute("SELECT count(*) FROM gold.hourly_issue_counts").fetchone()[0],
    }


def build_freshness(con: duckdb.DuckDBPyConnection, freshness: list[dict], run_id: str) -> None:
    """Dashboard tables: feed freshness + DQ summary of the latest run."""
    con.execute("DROP TABLE IF EXISTS gold.data_freshness")
    con.execute(
        """CREATE TABLE gold.data_freshness (
               source VARCHAR, status VARCHAR, last_fetch_utc TIMESTAMP,
               last_published_utc TIMESTAMP, rows BIGINT, checked_utc TIMESTAMP)"""
    )
    for f in freshness:
        con.execute(
            """INSERT INTO gold.data_freshness
               SELECT ?, ?, ?, max(published_utc), count(*), now()::TIMESTAMP
               FROM silver.articles WHERE source = ?""",
            [f["source"], f["status"], f["last_fetch_utc"], f["source"]],
        )
    con.execute(
        f"""CREATE OR REPLACE VIEW gold.dq_summary AS
            SELECT action, count(*) AS n_checks, sum(rows_affected) AS rows_affected
            FROM quality_log WHERE run_id = '{run_id}' GROUP BY action"""
    )
