from __future__ import annotations

from datetime import datetime

import pytest

from civicpulse.rag import generator, index, retriever
from civicpulse.rag.documents import RAGDocument, split_into_chunks, stable_id
from civicpulse.rag.sources import _visible_page_text


def _doc(**overrides):
    values = {
        "document_id": "doc-1",
        "title": "Water service notice",
        "source_url": "https://example.gov/water",
        "publisher": "Example Department",
        "published_at": None,
        "accessed_at": "2026-10-21",
        "location": "Karachi",
        "text": "Residents can report water service issues by phone.",
        "chunk_id": "chunk-1",
    }
    values.update(overrides)
    return RAGDocument(**values)


def test_rag_document_chunking_and_metadata_are_stable():
    chunks = split_into_chunks("alpha beta gamma delta", chunk_size=12, overlap=3)

    assert chunks[0] == "alpha beta"
    assert chunks[1].startswith("beta")
    assert stable_id("a", "b") == stable_id("a", "b")
    assert stable_id("a", "b") != stable_id("b", "a")

    record = _doc()
    metadata = record.as_vector_metadata()
    assert metadata["source_url"] == "https://example.gov/water"
    assert metadata["published_at"] == ""
    assert "text" not in metadata


def test_gold_and_forecast_rows_keep_distinct_evidence_types_and_source_ids():
    observed = retriever.gold_row_to_document(
        ("h_123", datetime(2026, 10, 21, 4), "Karachi", "Water", 7, "m1", "bronze/a.json")
    )

    assert observed.evidence_type == "gold_row"
    assert observed.row_id == "h_123"
    assert observed.table_name == "gold.hourly_issue_counts"
    assert "not confirmed incidents" in observed.text


def test_html_parser_excludes_navigation_and_script_content():
    title, text = _visible_page_text(
        b"<html><head><title>Service</title></head><body>"
        b"<nav>Menu</nav><main><p>Report water outages.</p></main>"
        b"<script>ignore me</script></body></html>",
        "text/html",
    )

    assert title == "Service"
    assert "Report water outages." in text
    assert "Menu" not in text
    assert "ignore me" not in text


def test_gold_and_latest_poisson_records_are_loaded_from_duckdb(monkeypatch, tmp_path):
    import duckdb

    database = tmp_path / "rag_gold.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE SCHEMA gold")
    con.execute(
        """CREATE TABLE gold.hourly_issue_counts (
            row_id VARCHAR, hour_utc TIMESTAMP, city VARCHAR, issue_type VARCHAR,
            n_reports BIGINT, manifest_ids VARCHAR, bronze_file_paths VARCHAR
        )"""
    )
    con.execute(
        "INSERT INTO gold.hourly_issue_counts VALUES "
        "('h_water', '2026-10-20 08:00:00', 'Karachi', 'Water', 4, 'm1', 'bronze/a.json')"
    )
    con.execute(
        """CREATE TABLE gold.poisson_forecasts (
            forecast_run_id VARCHAR, generated_at_utc TIMESTAMP, forecast_for_utc TIMESTAMP,
            city VARCHAR, issue_type VARCHAR, expected_reports DOUBLE,
            lower_95 BIGINT, upper_95 BIGINT, training_cutoff_utc TIMESTAMP,
            training_rows BIGINT, alpha DOUBLE, temperature_2m DOUBLE,
            precipitation DOUBLE, wind_speed_10m DOUBLE, weather_source_url VARCHAR
        )"""
    )
    con.execute(
        "INSERT INTO gold.poisson_forecasts VALUES "
        "('run_new', '2026-10-21 09:00:00', '2026-10-21 10:00:00', 'Karachi', 'Water', "
        "2.5, 1, 5, '2026-10-21 09:00:00', 100, 0.5, 30, 0, 10, 'https://weather.example/api'), "
        "('run_old', '2026-10-21 08:00:00', '2026-10-21 09:00:00', 'Karachi', 'Water', "
        "1.5, 0, 4, '2026-10-21 08:00:00', 90, 1.0, 29, 0, 9, NULL)"
    )
    con.close()
    monkeypatch.setattr(retriever.db, "connect", lambda: duckdb.connect(str(database)))

    observed = retriever._gold_context("Karachi Water", limit=5)
    forecast_rows = retriever._poisson_forecast_context("Karachi Water", limit=5)

    assert len(observed) == 1
    assert observed[0].row_id == "h_water"
    assert observed[0].n_reports == 4
    assert len(forecast_rows) == 1
    assert forecast_rows[0].row_id.startswith("run_new_Karachi_Water_")
    assert "Expected reports: 2.50" in forecast_rows[0].text
    assert "95% prediction interval: 1 to 5" in forecast_rows[0].text
    assert "not a confirmed incident count" in forecast_rows[0].text


def test_retrieve_evidence_combines_docs_gold_and_poisson_without_network(monkeypatch):
    class Collection:
        def count(self):
            return 1

        def query(self, **kwargs):
            assert kwargs["n_results"] == 1
            return {
                "documents": [["Official guidance"]],
                "metadatas": [[{"title": "Guidance", "evidence_type": "document"}]],
                "distances": [[0.2]],
            }

    # Patch the symbol used by retriever: it imported open_collection directly,
    # so patching index.open_collection after import would not intercept the call.
    monkeypatch.setattr(retriever, "open_collection", lambda: (None, Collection()))
    monkeypatch.setattr(retriever, "_gold_context", lambda question, limit: [
        _doc(evidence_type="gold_row", row_id="h_1")
    ])
    monkeypatch.setattr(retriever, "_poisson_forecast_context", lambda question, limit: [
        _doc(evidence_type="model_forecast", row_id="f_1")
    ])

    result = retriever.retrieve_evidence("Karachi Water reports", top_k=1)

    assert [item["metadata"]["evidence_type"] for item in result] == [
        "document", "gold_row", "model_forecast"
    ]
    assert result[1]["metadata"]["row_id"] == "h_1"
    assert result[2]["metadata"]["row_id"] == "f_1"


def test_retrieval_validates_question_and_top_k():
    with pytest.raises(ValueError, match="must not be empty"):
        retriever.retrieve_evidence("  ")
    with pytest.raises(ValueError, match="top_k"):
        retriever.retrieve_evidence("water", top_k=21)


def test_index_build_uses_archived_and_project_docs_and_reports_counts(monkeypatch):
    website_doc = _doc(source_url="https://weather.example/", evidence_type="document")
    pdf_doc = _doc(source_url="localfile://data/pdf/PCP.pdf", evidence_type="document")
    project_doc = _doc(source_url="repository://MODEL_CARD.md", evidence_type="project_document")

    class Collection:
        def __init__(self):
            self.saved = []

        def upsert(self, **kwargs):
            self.saved.append(kwargs)

    collection = Collection()
    monkeypatch.setattr(index, "fetch_source_pages", lambda: ([], ["site unavailable"]))
    monkeypatch.setattr(index, "archived_source_documents", lambda: [website_doc, pdf_doc])
    monkeypatch.setattr(index, "project_reference_documents", lambda: [project_doc])
    monkeypatch.setattr(index, "open_collection", lambda reset=False: (None, collection))

    result = index.build_index()

    assert result == {
        "documents": 3,
        "website_pages": 1,
        "local_pdfs": 1,
        "warnings": ["site unavailable"],
    }
    assert len(collection.saved) == 1
    assert len(collection.saved[0]["ids"]) == 3


def test_generator_returns_default_without_evidence_or_api_call(monkeypatch):
    monkeypatch.setattr(generator, "retrieve_evidence", lambda *args, **kwargs: [])
    monkeypatch.setattr(generator.requests, "post", lambda *args, **kwargs: pytest.fail("must not call LLM"))

    assert generator.answer_question("unrelated question") == generator.FALLBACK


def test_generator_sends_grounded_context_and_returns_citations(monkeypatch):
    evidence = [{
        "text": "Residents can call the official service line.",
        "metadata": {
            "evidence_type": "document",
            "title": "Water service notice",
            "publisher": "Example Department",
            "source_url": "https://example.gov/water",
            "chunk_id": "chunk-1",
        },
        "distance": 0.1,
    }]
    monkeypatch.setattr(generator, "retrieve_evidence", lambda *args, **kwargs: evidence)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Call the official line [S1].\n- Use the published service notice."}}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(generator.requests, "post", fake_post)

    answer = generator.answer_question("How can I report a water issue?")

    assert answer.startswith("Call the official line [S1].")
    assert "Reference: [S1] [Water service notice](https://example.gov/water)." in answer
    assert "\n" not in answer
    assert "- " not in answer
    assert "only the supplied evidence" in captured["json"]["messages"][0]["content"]
    assert "Residents can call" in captured["json"]["messages"][1]["content"]
    assert captured["headers"]["Authorization"] == "Bearer test-key"


def test_generator_discards_weak_document_matches_before_calling_llm(monkeypatch):
    monkeypatch.setattr(generator, "retrieve_evidence", lambda *args, **kwargs: [{
        "text": "unrelated", "metadata": {"evidence_type": "document"}, "distance": 0.99
    }])
    monkeypatch.setattr(generator.requests, "post", lambda *args, **kwargs: pytest.fail("weak evidence must be refused"))

    assert generator.answer_question("unrelated question") == generator.FALLBACK


def test_generator_requires_api_key_only_when_supported_evidence_exists(monkeypatch):
    monkeypatch.setattr(generator, "retrieve_evidence", lambda *args, **kwargs: [{
        "text": "supported", "metadata": {"evidence_type": "document"}, "distance": 0.1
    }])
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        generator.answer_question("question")
