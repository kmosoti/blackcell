"""Ephemeral SQLite FTS5 evidence retrieval."""

from blackcell.adapters.retrieval.fts5.adapter import (
    Fts5EvidenceRetriever,
    Fts5RetrievalError,
)

__all__ = ["Fts5EvidenceRetriever", "Fts5RetrievalError"]
