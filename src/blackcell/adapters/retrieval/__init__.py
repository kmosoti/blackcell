"""Replaceable retrieval adapters behind Blackcell-owned evidence policy."""

from blackcell.adapters.retrieval.fts5 import (
    Fts5EvidenceRetriever,
    Fts5RetrievalError,
)

__all__ = ["Fts5EvidenceRetriever", "Fts5RetrievalError"]
