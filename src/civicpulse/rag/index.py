"""Build the local Chroma index for trusted reference text; no answer generation happens here."""
from __future__ import annotations

from civicpulse import config
from civicpulse.rag.documents import RAGDocument
from civicpulse.rag.sources import (
    archived_source_documents,
    fetch_source_pages,
    project_reference_documents,
)

COLLECTION_NAME = "civicpulse_evidence"


def _dependencies():
    """Load optional RAG packages only when an indexing/retrieval command is used."""
    try:
        # pyrefly: ignore[missing-import]
        import chromadb
        # pyrefly: ignore[missing-import]
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    except ImportError as exc:
        raise RuntimeError(
            'RAG dependencies are missing. Install them with: python -m pip install -e ".[rag]"'
        ) from exc
    return chromadb, SentenceTransformerEmbeddingFunction


def embedding_function():
    """Create the local multilingual embedding model used for both indexing and queries."""
    _, sentence_transformer_function = _dependencies()
    return sentence_transformer_function(model_name=config.RAG_MODEL_NAME, device="cpu")


def open_collection(*, reset: bool = False):
    """Open the persistent evidence collection, optionally clearing stale document chunks."""
    chromadb, _ = _dependencies()
    client = chromadb.PersistentClient(path=str(config.RAG_VECTORSTORE_DIR))
    if reset:
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_function(),
        metadata={"hnsw:space": "cosine"},
    )
    return client, collection


def _index_records(collection, records: list[RAGDocument]) -> int:
    """Embed normalized text chunks and persist their citation metadata in Chroma."""
    if not records:
        return 0
    collection.upsert(
        ids=[f"{record.document_id}_{record.chunk_id}" for record in records],
        documents=[record.text for record in records],
        metadatas=[record.as_vector_metadata() for record in records],
    )
    return len(records)


def build_index() -> dict:
    """Fetch selected official pages, add project references, and rebuild the local index."""
    _, warnings = fetch_source_pages()
    # Re-read the latest checksum-verified Bronze snapshot for each URL. If a site is
    # temporarily unavailable, this keeps its last good text in the rebuilt index.
    archived_records = archived_source_documents()
    project_records = project_reference_documents()
    all_records = archived_records + project_records
    if not all_records:
        raise RuntimeError("No web or project documents were available to index.")

    _, collection = open_collection(reset=True)
    count = _index_records(collection, all_records)
    return {
        "documents": count,
        "website_pages": len({record.source_url for record in archived_records
                               if not record.source_url.startswith("localfile://")}),
        "local_pdfs": len({record.source_url for record in archived_records
                           if record.source_url.startswith("localfile://")}),
        "warnings": warnings,
    }
