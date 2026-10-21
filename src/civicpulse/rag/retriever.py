"""Retrieve reference chunks and exact, traceable Gold rows as context only."""
from __future__ import annotations

from civicpulse import config, db
from civicpulse.rag.documents import RAGDocument, utc_today
from civicpulse.rag.index import COLLECTION_NAME, open_collection


def _find_city_and_issue(question: str) -> tuple[str | None, str | None]:
    """Recognize exact configured city and issue names for a parameterized Gold query."""
    lowered = question.casefold()
    city = next((name for name in sorted(config.KNOWN_CITIES, key=len, reverse=True)
                 if name.casefold() in lowered), None)
    issue = next((name for name in config.ISSUE_PATTERNS if name.casefold() in lowered), None)
    return city, issue


def gold_row_to_document(row: tuple) -> RAGDocument:
    """Normalize a real hourly Gold row to the same metadata shape as document chunks."""
    row_id, hour_utc, city, issue_type, n_reports, sources, files = row
    hour = hour_utc.isoformat() if hasattr(hour_utc, "isoformat") else str(hour_utc)
    body = (
        f"Observed CivicPulse report count: {n_reports}. City: {city}. Issue type: {issue_type}. "
        f"Hour (UTC): {hour}. This is a count of news reports, not confirmed incidents."
    )
    return RAGDocument(
        document_id=f"gold_{row_id}",
        title="CivicPulse observed hourly report count",
        source_url=f"gold://gold.hourly_issue_counts/{row_id}",
        publisher="CivicPulse Gold warehouse",
        published_at=hour,
        accessed_at=utc_today(),
        location=city,
        text=body,
        chunk_id=f"chunk_{row_id}",
        evidence_type="gold_row",
        row_id=row_id,
        table_name="gold.hourly_issue_counts",
        city=city,
        issue_type=issue_type,
        hour_utc=hour,
        n_reports=int(n_reports),
        license_terms="Derived from CivicPulse Gold; see dataset declaration for source terms.",
        manifest_ids=sources or "",
        bronze_file_paths=files or "",
    )


def _gold_context(question: str, limit: int) -> list[RAGDocument]:
    """Read exact observed counts from Gold; never infer a number from vector similarity."""
    city, issue = _find_city_and_issue(question)
    if city is None and issue is None:
        return []
    con = db.connect()
    try:
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='gold' AND table_name='hourly_issue_counts'"
        ).fetchone()[0]
        if not exists:
            return []
        clauses = []
        params: list[object] = []
        if city:
            clauses.append("city = ?")
            params.append(city)
        if issue:
            clauses.append("issue_type = ?")
            params.append(issue)
        params.append(limit)
        rows = con.execute(
            "SELECT row_id, hour_utc, city, issue_type, n_reports, manifest_ids, bronze_file_paths "
            "FROM gold.hourly_issue_counts WHERE " + " AND ".join(clauses) +
            " ORDER BY hour_utc DESC LIMIT ?",
            params,
        ).fetchall()
        return [gold_row_to_document(row) for row in rows]
    finally:
        con.close()


def _poisson_forecast_context(question: str, limit: int) -> list[RAGDocument]:
    """Read the latest saved next-hour/day forecast for a named city and issue."""
    city, issue = _find_city_and_issue(question)
    if city is None or issue is None:
        return []

    con = db.connect()
    try:
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='gold' AND table_name='poisson_forecasts'"
        ).fetchone()[0]
        if not exists:
            return []
        rows = con.execute(
            """SELECT forecast_run_id, generated_at_utc, forecast_for_utc, city, issue_type,
                      expected_reports, lower_95, upper_95, training_cutoff_utc, training_rows,
                      alpha, temperature_2m, precipitation, wind_speed_10m, weather_source_url
               FROM gold.poisson_forecasts
               WHERE forecast_run_id = (
                   SELECT forecast_run_id FROM gold.poisson_forecasts
                   ORDER BY generated_at_utc DESC LIMIT 1
               ) AND city = ? AND issue_type = ?
               ORDER BY forecast_for_utc LIMIT ?""",
            [city, issue, limit],
        ).fetchall()
    finally:
        con.close()

    documents: list[RAGDocument] = []
    for row in rows:
        (run_id, generated, hour, row_city, row_issue, mean, low, high, cutoff,
         train_rows, alpha, temperature, precipitation, wind_speed, weather_url) = row
        hour = hour.isoformat() if hasattr(hour, "isoformat") else str(hour)
        generated = generated.isoformat() if hasattr(generated, "isoformat") else str(generated)
        cutoff = cutoff.isoformat() if hasattr(cutoff, "isoformat") else str(cutoff)
        row_id = f"{run_id}_{row_city}_{row_issue}_{hour}"
        body = (
            "CivicPulse Poisson model forecast for future hourly news-report counts; "
            "this is a model estimate, not a confirmed incident count. "
            f"City: {row_city}. Issue: {row_issue}. Forecast hour UTC: {hour}. "
            f"Expected reports: {float(mean):.2f}; 95% prediction interval: {int(low)} to {int(high)}. "
            f"Forecast generated UTC: {generated}. Training data cutoff UTC: {cutoff}. "
            f"Training rows: {int(train_rows)}. Selected alpha: {float(alpha)}. "
            f"Weather features used (temperature °C, precipitation mm, wind speed km/h): "
            f"{temperature}, {precipitation}, {wind_speed}. "
            f"Weather input source: {weather_url or 'not available; model imputed missing weather'}."
        )
        documents.append(RAGDocument(
            document_id=f"poisson_forecast_{row_id}",
            title="CivicPulse Poisson hourly forecast",
            source_url=f"model://gold.poisson_forecasts/{row_id}",
            publisher="CivicPulse hourly forecasting pipeline",
            published_at=hour,
            accessed_at=utc_today(),
            location=row_city,
            text=body,
            chunk_id=f"poisson_forecast_{row_id}",
            evidence_type="model_forecast",
            row_id=row_id,
            table_name="gold.poisson_forecasts",
            city=row_city,
            issue_type=row_issue,
            hour_utc=hour,
            license_terms="CivicPulse model output; forecast of news-report counts, not confirmed events.",
        ))
    return documents


def retrieve_evidence(question: str, top_k: int = 5) -> list[dict]:
    """Return retrieved documents and vector distances for grounded generation."""
    if not question.strip():
        raise ValueError("Question must not be empty.")
    if top_k < 1 or top_k > 20:
        raise ValueError("top_k must be between 1 and 20.")

    _, collection = open_collection()
    if collection.count() == 0:
        raise RuntimeError("RAG index is empty. Run: python -m civicpulse rag-index")

    hits = collection.query(
        query_texts=[question],
        n_results=min(top_k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )
    evidence: list[dict] = []
    for text, metadata, distance in zip(
        hits["documents"][0], hits["metadatas"][0], hits["distances"][0]
    ):
        evidence.append({"text": text, "metadata": metadata, "distance": float(distance)})

    # Exact Gold rows are appended separately so every numeric claim has a stable row ID.
    for record in _gold_context(question, limit=top_k):
        evidence.append({"text": record.text, "metadata": record.as_vector_metadata(), "distance": None})
    # Add saved future model output only for questions naming both a known city and issue.
    for record in _poisson_forecast_context(question, limit=min(top_k, 24)):
        evidence.append({"text": record.text, "metadata": record.as_vector_metadata(), "distance": None})
    return evidence


def retrieve_context(question: str, top_k: int = 5) -> str:
    """Return evidence blocks only; this function does not call a generative language model."""
    blocks: list[str] = []
    for item in retrieve_evidence(question, top_k):
        metadata, text = item["metadata"], item["text"]
        if metadata.get("evidence_type") in {"gold_row", "model_forecast"}:
            blocks.append(
                "[CIVICPULSE STRUCTURED MODEL/DATA RESULT]\n"
                f"Table: {metadata.get('table_name')}\nRow ID: {metadata.get('row_id')}\n"
                f"Time UTC: {metadata.get('hour_utc')}\nCity: {metadata.get('city')}\n"
                f"Issue type: {metadata.get('issue_type')}\nText: {text}"
            )
        else:
            blocks.append(
                "[REFERENCE DOCUMENT]\n"
                f"Title: {metadata.get('title', '')}\nPublisher: {metadata.get('publisher', '')}\n"
                f"Source: {metadata.get('source_url', '')}\nSection/chunk: {metadata.get('chunk_id', '')}\n"
                f"Location: {metadata.get('location', '')}\nText: {text}"
            )
    return "\n\n---\n\n".join(blocks)
