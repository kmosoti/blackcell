from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing

from blackcell.features.retrieve_evidence import (
    EvidenceClaimIdentity,
    EvidenceObjectiveMatch,
    EvidenceSelection,
    RankedEvidenceRetriever,
    RetrieveEvidence,
)
from blackcell.features.retrieve_evidence.ports import SignalClaimLike, SignalPacketLike
from blackcell.kernel._json import canonical_json

_OBJECTIVE_TERM = re.compile(r"[\w./:-]+")
_FAILURE = "SQLite FTS5 evidence retrieval failed"


class Fts5RetrievalError(RuntimeError):
    """FTS5 is unavailable or did not return a valid bounded ranking."""


class Fts5EvidenceRetriever:
    """Rank one immutable SignalPacket using an ephemeral in-memory FTS5 index."""

    def __init__(self) -> None:
        self._retriever = RankedEvidenceRetriever(_Fts5ObjectiveMatcher())

    def handle(
        self,
        query: RetrieveEvidence,
        packet: SignalPacketLike,
    ) -> EvidenceSelection:
        return self._retriever.handle(query, packet)


class _Fts5ObjectiveMatcher:
    def match(
        self,
        objective: str,
        claims: Sequence[SignalClaimLike],
    ) -> tuple[EvidenceObjectiveMatch, ...]:
        expression = _match_expression(objective)
        if not expression or not claims:
            return ()
        try:
            with closing(sqlite3.connect(":memory:")) as connection:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE evidence USING fts5(
                        source_event_id UNINDEXED,
                        claim_id UNINDEXED,
                        subject,
                        predicate,
                        value,
                        tokenize='unicode61'
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO evidence(source_event_id, claim_id, subject, predicate, value)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            claim.source_event_id,
                            claim.claim_id,
                            claim.subject,
                            claim.predicate,
                            canonical_json(claim.value),
                        )
                        for claim in claims
                    ),
                )
                rows = connection.execute(
                    """
                    SELECT source_event_id, claim_id
                    FROM evidence
                    WHERE evidence MATCH ?
                    ORDER BY bm25(evidence), source_event_id, claim_id
                    """,
                    (expression,),
                ).fetchall()
        except sqlite3.Error, TypeError, ValueError:
            raise Fts5RetrievalError(_FAILURE) from None
        return tuple(
            EvidenceObjectiveMatch(
                identity=EvidenceClaimIdentity(source_event_id, claim_id),
                score=100 * (len(rows) - index),
                reason="fts5-objective-match",
            )
            for index, (source_event_id, claim_id) in enumerate(rows)
        )


def _match_expression(objective: str) -> str:
    terms = sorted(set(_OBJECTIVE_TERM.findall(objective.casefold())))
    return " OR ".join(f'"{term}"' for term in terms)


__all__ = ["Fts5EvidenceRetriever", "Fts5RetrievalError"]
