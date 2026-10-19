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

SOURCES = {"newsagent", "gdelt"}
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
    return int((old.fillna("\x00").astype(str) != new.fillna("\x00").astype(str)).sum())


class DataQualityAgent:
    def __init__(self, con: duckdb.DuckDBPyConnection, run_id: str):
        self.con, self.run_id = con, run_id
        self.logs: list[dict] = []
        self.freshness: list[dict] = []

    # ---------------------------------------------------------------- logging
    def log(self, check: str, action: str, rows: int = 0, detail: str = "",
            layer: str = "silver", table: str = "silver.articles") -> None:
        self.logs.append({
            "run_id": self.run_id, "logged_at_utc": datetime.now(timezone.utc).replace(tzinfo=None),
            "layer": layer, "table_name": table, "check_name": check, "action": action,
            "rows_affected": int(rows), "detail": detail,
        })

    def flush(self) -> None:
        if self.logs:
            df = pd.DataFrame(self.logs)
            self.con.execute("INSERT INTO quality_log SELECT * FROM df")
            self.logs = []

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _mark(df: pd.DataFrame, mask: pd.Series, col: str, token: str) -> None:
        cur = df.loc[mask, col].fillna("")
        df.loc[mask, col] = cur.map(lambda x: f"{x};{token}" if x else token)

    # ---------------------------------------------------------------- FIX
    def fix(self, df: pd.DataFrame) -> pd.DataFrame:
        for col in ("title", "url", "city", "province", "service"):
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
        df["_prio"] = df["source"].map({"newsagent": 0, "gdelt": 1}).fillna(2)
        df = df.sort_values(["_prio", "published_utc"], kind="stable")
        dup = df["article_id"].notna() & df.duplicated("article_id", keep="first")
        n = int(dup.sum())
        self.log("duplicate_links", "fix" if n else "pass", n, "kept newsagent over gdelt, then earliest")
        return df[~dup].drop(columns="_prio").reset_index(drop=True)

    # ---------------------------------------------------------------- FILL
    def fill(self, df: pd.DataFrame) -> pd.DataFrame:
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
        now = pd.Timestamp.now("UTC").tz_localize(None) + pd.Timedelta(days=1)
        lo = pd.Timestamp(config.MIN_PUBLISHED)
        C = lambda fn, name: pa.Check(fn, name=name, ignore_na=False)  # noqa: E731
        spec = {
            "source": [C(lambda s: s.isin(SOURCES), "source_valid")],
            "source_record_id": [C(lambda s: s.notna(), "record_id_present")],
            "url": [C(lambda s: s.fillna("").astype(str).str.match(r"^https?://[^\s/]+\.[^\s/]+"), "url_present_http")],
            "published_utc": [
                C(lambda s: s.notna(), "published_present"),
                C(lambda s: s.isna() | ((s >= lo) & (s <= now)), "published_in_range"),
            ],
            "urgency_score": [C(lambda s: s.isna() | s.between(0, 10), "urgency_0_to_10")],
            "article_id": [C(lambda s: s.isna() | ~s.duplicated(keep=False), "article_id_unique")],
        }
        schema = pa.DataFrameSchema(
            {c: pa.Column(checks=checks, nullable=True, required=True) for c, checks in spec.items()},
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
        fc = fc[fc["index"].notna()].copy()
        fc["index"] = fc["index"].astype(int)
        for name in all_checks:
            n = int(fc.loc[fc["check"] == name, "index"].nunique())
            self.log(name, "quarantine" if n else "pass", n)
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

        lat, lon = df["lat"], df["lon"]
        impossible = (lat.notna() & (lat.abs() > 90)) | (lon.notna() & (lon.abs() > 180))
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
        live = df[df["source"] == "newsagent"]
        if live.empty:
            self.log("stale_feed:newsagent", "flag", 0, "no NewsAgent rows loaded")
            status, last = "NO_DATA", None
        else:
            last = live["fetched_utc"].max()
            age_h = (now - last).total_seconds() / 3600
            stale = age_h > config.STALE_AFTER_HOURS
            status = "STALE" if stale else "FRESH"
            self.log("stale_feed:newsagent", "flag" if stale else "pass", 0,
                     f"last pull {age_h:.1f}h ago (limit {config.STALE_AFTER_HOURS}h)")
        self.freshness.append({"source": "newsagent", "status": status, "last_fetch_utc": last})

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
