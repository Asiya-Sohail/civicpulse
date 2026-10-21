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


def _poisson_backtest_context(question: str, limit: int) -> list[RAGDocument]:
    """Return the model's held-out predictions for an explicitly named city and issue."""
    city, issue = _find_city_and_issue(question)
    if city is None or issue is None:
        return []

    # The current modeling pipeline produces historical test predictions, not live forecasts.
    from civicpulse.modeling import poisson_model

    con = db.connect()
    try:
        selected = poisson_model.select_poisson_alpha(
            con=con, cutoff_utc=config.FORECAST_CUTOFF_UTC
        )
        evaluated = poisson_model.evaluate_poisson_model(
            con=con,
            cutoff_utc=config.FORECAST_CUTOFF_UTC,
            alpha=float(selected["selected_alpha"]),
        )
    finally:
        con.close()

    predictions = poisson_model.add_poisson_prediction_intervals(evaluated["predictions"])
    selected_rows = predictions[
        (predictions["city"] == city) & (predictions["issue_type"] == issue)
    ].sort_values("hour_utc", ascending=False).head(limit)

    documents: list[RAGDocument] = []
    for row in selected_rows.itertuples(index=False):
        hour = row.hour_utc.isoformat() if hasattr(row.hour_utc, "isoformat") else str(row.hour_utc)
        row_id = f"{city}_{issue}_{hour}"
        body = (
            "Poisson model held-out backtest prediction (not a future/live forecast). "
            f"City: {city}. Issue: {issue}. Test hour UTC: {hour}. "
            f"Predicted report count: {row.prediction:.2f}; 95% prediction interval: "
            f"{int(row.prediction_lower)} to {int(row.prediction_upper)}. "
            f"Observed report count for evaluation: {int(row.n_reports)}. "
            f"Absolute error: {row.absolute_error:.2f}. "
            f"Training cutoff UTC: {config.FORECAST_CUTOFF_UTC}."
        )
        documents.append(RAGDocument(
            document_id=f"poisson_backtest_{row_id}",
            title="CivicPulse Poisson model held-out backtest result",
            source_url=f"model://poisson_backtest/{row_id}",
            publisher="CivicPulse model evaluation",
            published_at=hour,
            accessed_at=utc_today(),
            location=city,
            text=body,
            chunk_id=f"poisson_backtest_{row_id}",
            evidence_type="model_backtest",
            row_id=row_id,
            table_name="model_evaluation.poisson_backtest_predictions (computed at retrieval time)",
            city=city,
            issue_type=issue,
            hour_utc=hour,
            n_reports=int(row.n_reports),
            license_terms="CivicPulse model output; held-out historical evaluation, not a live forecast.",
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
    # Add actual model output only for questions that name both a known city and issue.
    for record in _poisson_backtest_context(question, limit=min(top_k, 12)):
        evidence.append({"text": record.text, "metadata": record.as_vector_metadata(), "distance": None})
    return evidence


def retrieve_context(question: str, top_k: int = 5) -> str:
    """Return evidence blocks only; this function does not call a generative language model."""
    blocks: list[str] = []
    for item in retrieve_evidence(question, top_k):
        metadata, text = item["metadata"], item["text"]
        if metadata.get("evidence_type") in {"gold_row", "model_backtest"}:
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
