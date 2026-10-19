"""Data Quality Agent (DQ gate).

Runs on every pipeline run and logs every action to the `quality_log` table:
  FIX         rule-based correction (whitespace, city aliases, URL canonicalisation, duplicates)
  FILL        impute missing values, always marked is_imputed = True (+ which fields)
  FLAG        keep the row, mark it (unverified, impossible/out-of-country coords, wrong country tag)
  QUARANTINE  rows that fail hard schema checks go to silver.quarantine, not downstream
  PASS        an assertion that found nothing wrong (logged so the audit trail is complete)

Hard checks are Pandera assertions with explicit names so each one is traceable in the log.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import hashlib
import re

import duckdb
import numpy as np
import pandas as pd

try:  # pandera >= 0.24 prefers the explicit pandas namespace
    import pandera.pandas as pa
except ImportError:  # pragma: no cover
    import pandera as pa

from civicpulse import config

SOURCES = {"freenewsapi", "gdelt"}
DRIFT_THRESHOLD = 0.20
_TRACKING = re.compile(r"^(utm_|mc_)|^(fbclid|gclid|igshid)$", re.I)


def canonical_url(u):
    """Lower-case host, drop fragment + tracking params + trailing slash. Keeps the scheme."""
    if not isinstance(u, str) or not u.strip():
        return None
    s = u.strip()
    try:
        p = urlsplit(s)
    except ValueError:
        return s
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        return s
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)]
    path = p.path.rstrip("/") or ""
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(sorted(q)), ""))


def dedupe_key(u):
    """Same article whether http/https or with/without www."""
    if not isinstance(u, str):
        return None
    p = urlsplit(u)
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path}?{p.query}" if host else None


def publisher_domain(u):
    if not isinstance(u, str):
        return None
    host = urlsplit(u).netloc.lower().removeprefix("www.")
    return host or None


def _norm_text(v):
    if not isinstance(v, str):
        return v
    v = re.sub(r"\s+", " ", v).strip()
    return v or None


def _changed(old: pd.Series, new: pd.Series) -> int:
    def comparable(series: pd.Series) -> list[str | None]:
        return [None if pd.isna(value) else str(value) for value in series]
    before, after = comparable(old), comparable(new)
    return sum(left != right for left, right in zip(before, after))


class DataQualityAgent:
    def __init__(self, con: duckdb.DuckDBPyConnection, run_id: str):
        self.con, self.run_id = con, run_id
        self.logs: list[dict] = []
        self.freshness: list[dict] = []
        self._baseline_to_persist: pd.DataFrame | None = None

    # ---------------------------------------------------------------- logging
    def log(self, check: str, action: str, rows: int = 0, detail: str = "",
            layer: str = "silver", table: str = "silver.articles") -> None:
        self.logs.append({
            "run_id": self.run_id, "logged_at_utc": datetime.now(timezone.utc).replace(tzinfo=None),
            "layer": layer, "table_name": table, "check_name": check, "rule_name": check,
            "action": action, "rows_affected": int(rows), "detail": detail,
            "reason": detail or (
                f"Rule {check} passed with no affected rows."
                if action == "pass" else f"Rule {check} applied action {action} to {int(rows)} row(s)."
            ),
        })

    def flush(self) -> None:
        if self.logs:
            df = pd.DataFrame(self.logs)
            columns = [
                "run_id", "logged_at_utc", "layer", "table_name", "check_name",
                "rule_name", "action", "rows_affected", "detail", "reason",
            ]
            self.con.execute(
                f"INSERT INTO quality_log ({', '.join(columns)}) "
                f"SELECT {', '.join(columns)} FROM df"
            )
            self.logs = []

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _mark(df: pd.DataFrame, mask: pd.Series, col: str, token: str) -> None:
        cur = df.loc[mask, col].fillna("")
        df.loc[mask, col] = cur.map(lambda x: f"{x};{token}" if x else token)

    # ---------------------------------------------------------------- FIX
    def fix(self, df: pd.DataFrame) -> pd.DataFrame:
        for col in ("title", "url", "publisher", "city", "province", "service"):
            old = df[col].copy()
            df[col] = df[col].map(_norm_text)
            n = _changed(old, df[col])
            self.log(f"trim_whitespace:{col}", "fix" if n else "pass", n)

        old = df["city"].copy()
        def fix_city(v):
            if not isinstance(v, str):
                return v
            return config.CITY_ALIASES.get(v.lower(), v.title())
        df["city"] = df["city"].map(fix_city)
        n_alias = int(sum(isinstance(a, str) and a.lower() in config.CITY_ALIASES for a in old))
        n = _changed(old, df["city"])
        self.log("city_normalise", "fix" if n else "pass", n, f"{n_alias} alias hits (e.g. KHI -> Karachi)")

        old = df["url"].copy()
        df["url"] = df["url"].map(canonical_url)
        n = _changed(old, df["url"])
        self.log("url_canonicalise", "fix" if n else "pass", n, "lower-case host, strip utm/fbclid/fragment/trailing slash")

        df["publisher_domain"] = df["url"].map(publisher_domain)
        df["article_id"] = df["url"].map(
            lambda u: hashlib.sha256(dedupe_key(u).encode()).hexdigest()[:16] if dedupe_key(u) else None
        )
        return df

    def dedupe(self, df: pd.DataFrame) -> pd.DataFrame:
        df["_prio"] = df["source"].map({"freenewsapi": 0, "gdelt": 1}).fillna(2)
        df = df.sort_values(["_prio", "published_utc"], kind="stable")
        dup = df["article_id"].notna() & df.duplicated("article_id", keep="first")
        n = int(dup.sum())
        self.log("duplicate_links", "fix" if n else "pass", n, "kept freenewsapi over gdelt, then earliest")
        return df[~dup].drop(columns="_prio").reset_index(drop=True)

    # ---------------------------------------------------------------- FILL
    def fill(self, df: pd.DataFrame) -> pd.DataFrame:
        m = df["publisher"].isna() & df["publisher_domain"].notna()
        df.loc[m, "publisher"] = df.loc[m, "publisher_domain"]
        df.loc[m, "is_imputed"] = True
        self._mark(df, m, "imputed_fields", "publisher")
        self.log(
            "fill_publisher_from_domain", "fill" if m.any() else "pass", int(m.sum()),
            "publisher name unavailable; copied the normalized URL host as a fallback",
        )

        m = df["published_utc"].isna() & df["fetched_utc"].notna()
        df.loc[m, "published_utc"] = df.loc[m, "fetched_utc"]
        df.loc[m, "is_imputed"] = True
        self._mark(df, m, "imputed_fields", "published_utc")
        self.log("fill_published_utc", "fill" if m.any() else "pass", int(m.sum()), "imputed from fetch time")

        observed = df["urgency_score"].notna()
        m = ~observed
        if observed.any():
            by_issue = df.loc[observed].groupby("issue_type")["urgency_score"].median()
            glob = float(df.loc[observed, "urgency_score"].median())
            df.loc[m, "urgency_score"] = df.loc[m, "issue_type"].map(by_issue).fillna(glob)
            df.loc[m, "is_imputed"] = True
            self._mark(df, m, "imputed_fields", "urgency_score")
            self.log("fill_urgency_score", "fill" if m.any() else "pass", int(m.sum()),
                     "median urgency of the same issue_type (fallback: global median)")
        else:
            self.log("fill_urgency_score", "flag", int(m.sum()), "no observed urgency scores to impute from")
        return df

    # ---------------------------------------------------------------- QUARANTINE
    def _schema(self) -> tuple[pa.DataFrameSchema, list[str]]:
        fetched_now = pd.Timestamp.now("UTC").tz_localize(None)
        now = fetched_now + pd.Timedelta(days=1)
        lo = pd.Timestamp(config.MIN_PUBLISHED)
        C = lambda fn, name: pa.Check(fn, name=name, ignore_na=False)  # noqa: E731
        spec = {
            "source": [C(lambda s: s.isin(SOURCES), "source_valid")],
            "source_record_id": [
                C(lambda s: s.notna() & s.astype(str).str.strip().ne(""), "record_id_present")
            ],
            "url": [
                C(lambda s: s.fillna("").astype(str).str.match(r"^https?://[^\s/]+\.[^\s/]+"), "url_present_http")
            ],
            "published_utc": [
                C(lambda s: s.notna(), "published_present"),
                C(lambda s: s.isna() | ((s >= lo) & (s <= now)), "published_in_range"),
            ],
            "fetched_utc": [
                C(lambda s: s.notna(), "fetch_time_present"),
                C(lambda s: s.isna() | (s <= fetched_now), "fetch_time_not_future"),
            ],
            "crawled_utc": [
                C(lambda s: s.isna() | (s <= fetched_now), "crawl_time_not_future")
            ],
            "urgency_score": [C(lambda s: s.isna() | s.between(0, 10), "urgency_0_to_10")],
            "article_id": [C(lambda s: s.isna() | ~s.duplicated(keep=False), "article_id_unique")],
            "bronze_file_path": [
                C(lambda s: s.notna() & s.astype(str).str.strip().ne(""), "bronze_file_present")
            ],
            "bronze_file_sha256": [
                C(lambda s: s.fillna("").astype(str).str.fullmatch(r"[0-9a-f]{64}"), "bronze_sha256_valid")
            ],
            "manifest_id": [
                C(lambda s: s.fillna("").astype(str).str.fullmatch(r"[0-9a-f]{64}"), "manifest_id_valid")
            ],
            "manifest_fetch_utc": [
                C(lambda s: s.notna() & (s <= fetched_now), "manifest_fetch_time_valid")
            ],
        }
        types = {
            "source": str, "source_record_id": str, "url": str, "published_utc": pa.DateTime,
            "urgency_score": float, "lat": float, "lon": float, "article_id": str,
            "bronze_file_path": str, "bronze_file_sha256": str, "manifest_id": str,
            "manifest_fetch_utc": pa.DateTime,
            "title": str, "publisher": str, "publisher_domain": str, "crawled_utc": pa.DateTime,
            "fetched_utc": pa.DateTime, "city": str, "province": str, "issue_type": str,
            "service": str, "cameo_code": str, "avg_tone": float, "raw_topic": str,
            "country_tag": str, "is_imputed": bool, "imputed_fields": str,
            "verification_status": str, "dq_flags": str, "manifest_source_url": str,
            "manifest_search_query": str, "manifest_license_terms": str,
        }
        spec.update({
            "title": [], "publisher": [], "publisher_domain": [], "crawled_utc": [],
            "fetched_utc": [], "city": [], "province": [], "issue_type": [], "service": [],
            "cameo_code": [], "avg_tone": [], "raw_topic": [], "country_tag": [],
            "is_imputed": [], "imputed_fields": [], "verification_status": [], "dq_flags": [],
            "manifest_source_url": [], "manifest_search_query": [], "manifest_license_terms": [],
        })
        schema = pa.DataFrameSchema(
            {
                c: pa.Column(dtype=types[c], checks=checks, nullable=True, required=True)
                for c, checks in spec.items()
            },
            strict=False,
        )
        return schema, [c.name for checks in spec.values() for c in checks]

    def validate(self, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        schema, all_checks = self._schema()
        try:
            schema.validate(df, lazy=True)
            fc = pd.DataFrame(columns=["check", "index"])
        except pa.errors.SchemaErrors as exc:
            fc = exc.failure_cases
        if not fc.empty and {"column", "check"} <= set(fc.columns):
            type_failure = fc["check"].astype(str).str.startswith("dtype(")
            fc.loc[type_failure, "check"] = fc.loc[type_failure, "column"].astype(str) + "_type"
        fc = fc[fc["index"].notna()].copy()
        fc["index"] = fc["index"].astype(int)
        for name in dict.fromkeys([*all_checks, *fc["check"].astype(str).tolist()]):
            n = int(fc.loc[fc["check"] == name, "index"].nunique())
            self.log(name, "quarantine" if n else "pass", n)
        title_missing = df["title"].isna()
        fna_title_missing = df["source"].eq("freenewsapi") & title_missing
        self.log(
            "missingness:title",
            "flag" if title_missing.any() else "pass",
            int(title_missing.sum()),
            f"{int(title_missing.sum() - fna_title_missing.sum())} GDELT title(s) unavailable by source design; "
            f"{int(fna_title_missing.sum())} FreeNewsAPI title(s) missing",
        )
        city_missing = df["city"].isna()
        self.log("missingness:city", "flag" if city_missing.any() else "pass",
                 int(city_missing.sum()), "city could not be resolved from article metadata or event geocoding")
        coordinates_missing = df["lat"].isna() | df["lon"].isna()
        self.log("missingness:coordinates", "flag" if coordinates_missing.any() else "pass",
                 int(coordinates_missing.sum()), "source does not provide complete coordinates for these records")
        crawled_missing = (df["source"] == "freenewsapi") & df["crawled_utc"].isna()
        self.log("missingness:crawled_utc", "flag" if crawled_missing.any() else "pass",
                 int(crawled_missing.sum()), "FreeNewsAPI crawl timestamp unavailable")
        if fc.empty:
            return df, df.iloc[0:0].assign(quarantine_reason=pd.Series(dtype=str), run_id=pd.Series(dtype=str))
        reasons = fc.groupby("index")["check"].apply(lambda s: ";".join(sorted(set(s))))
        bad = df.loc[reasons.index].copy()
        bad["quarantine_reason"] = reasons
        bad["run_id"] = self.run_id
        return df.drop(index=reasons.index).reset_index(drop=True), bad.reset_index(drop=True)

    # ---------------------------------------------------------------- FLAG
    def flag(self, df: pd.DataFrame) -> pd.DataFrame:
        df["verification_status"] = "unverified"
        self.log("unverified_news", "flag", len(df),
                 "every row is a *reported* disruption until matched to an official source")

        missing_title = (df["source"] == "freenewsapi") & df["title"].isna()
        self._mark(df, missing_title, "dq_flags", "MISSING_TITLE")
        self.log("missing_title:freenewsapi", "flag" if missing_title.any() else "pass",
                 int(missing_title.sum()), "row retained; title absence is recorded for review")

        lat, lon = df["lat"], df["lon"]
        invalid_lat = lat.notna() & ~lat.between(-90, 90)
        invalid_lon = lon.notna() & ~lon.between(-180, 180)
        impossible = invalid_lat | invalid_lon
        self.log(
            "coordinate_ranges",
            "flag" if impossible.any() else "pass",
            int(impossible.sum()),
            "out-of-range coordinates are nulled and retained for review",
        )
        self._mark(df, impossible, "dq_flags", "IMPOSSIBLE_COORD")
        df.loc[impossible, ["lat", "lon"]] = np.nan  # row kept, bad coordinates not trusted
        self.log("impossible_coordinates", "flag" if impossible.any() else "pass", int(impossible.sum()),
                 "coordinates nulled, row kept")

        la0, la1, lo0, lo1 = config.PK_BBOX
        has = df["lat"].notna() & df["lon"].notna()
        outside = has & ~(df["lat"].between(la0, la1) & df["lon"].between(lo0, lo1))
        self._mark(df, outside, "dq_flags", "COORD_OUTSIDE_PK")
        self.log("coords_inside_pakistan", "flag" if outside.any() else "pass", int(outside.sum()))

        tag = df["country_tag"]
        wrong = tag.notna() & ~tag.astype(str).str.contains(r"\bpk\b|pakistan", case=False, regex=True)
        self._mark(df, wrong, "dq_flags", "NOT_PK_TAG")
        self.log("country_tag_is_pakistan", "flag" if wrong.any() else "pass", int(wrong.sum()))

        self._freshness(df)
        return df

    def _freshness(self, df: pd.DataFrame) -> None:
        now = pd.Timestamp.now("UTC").tz_localize(None)
        live = df[df["source"] == "freenewsapi"]
        if live.empty:
            self.log("stale_feed:freenewsapi", "flag", 0, "no FreeNewsAPI rows loaded")
            status, last = "NO_DATA", None
        else:
            last = live["fetched_utc"].max()
            age_h = (now - last).total_seconds() / 3600
            stale = age_h > config.STALE_AFTER_HOURS
            status = "STALE" if stale else "FRESH"
            self.log("stale_feed:freenewsapi", "flag" if stale else "pass", len(live) if stale else 0,
                     f"last pull {age_h:.1f}h ago (limit {config.STALE_AFTER_HOURS}h)")
        self.freshness.append({"source": "freenewsapi", "status": status, "last_fetch_utc": last})

    def check_distribution(self, df: pd.DataFrame) -> None:
        """Compare source, issue, and city shares with the first successful-run baseline."""
        baseline = self.con.execute(
            """SELECT dimension, category, row_count, proportion
               FROM dq_distribution_baseline"""
        ).df()
        dimensions = ("source", "issue_type", "city")
        if df.empty:
            self.log("distribution_baseline", "flag", 0, "no Silver rows available for distribution checks")
            return

        current_rows = []
        for dimension in dimensions:
            values = df[dimension].fillna("__MISSING__").astype(str)
            counts = values.value_counts(dropna=False)
            for category, count in counts.items():
                current_rows.append({
                    "dimension": dimension, "category": category,
                    "row_count": int(count), "proportion": float(count / len(df)),
                })
        current = pd.DataFrame(current_rows)
        if baseline.empty:
            self._baseline_to_persist = current
            for dimension in dimensions:
                self.log(
                    f"distribution_baseline:{dimension}", "pass", 0,
                    f"first successful baseline scheduled from {len(df)} Silver rows",
                )
            return

        for dimension in dimensions:
            old = baseline[baseline["dimension"] == dimension].set_index("category")
            new = current[current["dimension"] == dimension].set_index("category")
            categories = sorted(set(old.index) | set(new.index))
            drifted = []
            for category in categories:
                old_share = float(old.loc[category, "proportion"]) if category in old.index else 0.0
                new_share = float(new.loc[category, "proportion"]) if category in new.index else 0.0
                delta = abs(new_share - old_share)
                if delta > DRIFT_THRESHOLD:
                    affected = int(new.loc[category, "row_count"]) if category in new.index else 0
                    drifted.append((category, old_share, new_share, affected))
                    self.log(
                        f"distribution_drift:{dimension}:{category}", "flag", affected,
                        f"share changed from {old_share:.3f} to {new_share:.3f} "
                        f"(absolute threshold {DRIFT_THRESHOLD:.2f})",
                    )
            if not drifted:
                self.log(
                    f"distribution_drift:{dimension}", "pass", 0,
                    f"all category shares are within absolute threshold {DRIFT_THRESHOLD:.2f}",
                )

    def persist_distribution_baseline(self) -> None:
        """Persist the initial baseline only after the complete Gold build passes."""
        if self._baseline_to_persist is None:
            return
        existing = self.con.execute("SELECT count(*) FROM dq_distribution_baseline").fetchone()[0]
        if existing:
            self._baseline_to_persist = None
            return
        baseline = self._baseline_to_persist.copy()
        baseline["baseline_run_id"] = self.run_id
        baseline["recorded_at_utc"] = datetime.now(timezone.utc).replace(tzinfo=None)
        self.con.execute(
            """INSERT INTO dq_distribution_baseline
               (dimension, category, row_count, proportion, baseline_run_id, recorded_at_utc)
               SELECT dimension, category, row_count, proportion, baseline_run_id, recorded_at_utc
               FROM baseline"""
        )
        self._baseline_to_persist = None

    # ---------------------------------------------------------------- orchestration
    def run(self, cand: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        df = cand.reset_index(drop=True).copy()
        df = self.fix(df)
        df = self.dedupe(df)
        df = self.fill(df)
        clean, quarantined = self.validate(df)
        clean = self.flag(clean)
        return clean, quarantined

    def gate_gold(self) -> None:
        """Assertions on the finished gold tables: ids unique and non-null, counts sane."""
        for table in ("gold.issue_events", "gold.hourly_issue_counts"):
            total, distinct, nulls = self.con.execute(
                f"SELECT count(*), count(DISTINCT row_id), count(*) FILTER (WHERE row_id IS NULL) FROM {table}"
            ).fetchone()
            ok = total == distinct and nulls == 0
            self.log("row_id_unique_not_null", "pass" if ok else "flag", 0 if ok else total - distinct,
                     f"{total} rows", layer="gold", table=table)
            if not ok:
                self.flush()
                raise RuntimeError(f"{table}: row_id not unique / has nulls")
