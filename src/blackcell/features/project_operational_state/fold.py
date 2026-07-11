from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast

from blackcell.features.project_operational_state.models import (
    BeliefClaim,
    BeliefConflict,
    BeliefCorrection,
    EpistemicStatus,
    OperationalBeliefState,
    OperationalStateScope,
    UnknownReason,
)
from blackcell.kernel import EventEnvelope, JsonInput, JsonScalar
from blackcell.kernel._json import canonical_json, json_digest

OBSERVATION_EVENT_TYPES = frozenset({"observation.recorded", "ObservationRecorded"})
CORRECTION_EVENT_TYPES = frozenset({"observation.corrected"})
STATE_EVENT_TYPES = OBSERVATION_EVENT_TYPES | CORRECTION_EVENT_TYPES
LEGACY_OBSERVATION_DOMAIN = "repository"
CORRECTION_SCHEMA_VERSION = "observation-correction/v1"
RAW_STATE_SCHEMA_VERSION = "operational-state-fold/v1"


@dataclass(frozen=True, slots=True)
class RawOperationalState:
    """Normalized ledger evidence cached by the disposable projection checkpoint."""

    scope: OperationalStateScope
    claims: tuple[BeliefClaim, ...]
    corrections: tuple[BeliefCorrection, ...]
    last_source_stream_sequence: int

    def __post_init__(self) -> None:
        if not self.scope.bound:
            raise ValueError("raw operational state requires a bound scope")
        if self.last_source_stream_sequence < 0:
            raise ValueError("last_source_stream_sequence must be non-negative")
        if any(
            claim.domain != self.scope.domain or claim.stream_id != self.scope.stream_id
            for claim in self.claims
        ):
            raise ValueError("raw claims must belong to the fold scope")
        if any(
            correction.domain != self.scope.domain or correction.stream_id != self.scope.stream_id
            for correction in self.corrections
        ):
            raise ValueError("raw corrections must belong to the fold scope")
        if any(
            claim.epistemic_status is not EpistemicStatus.OBSERVED
            or claim.unknown_reason is not None
            for claim in self.claims
        ):
            raise ValueError("raw checkpoints cannot contain derived unknown claims")
        claim_ids = tuple(claim.claim_id for claim in self.claims)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("raw claim ids must be unique")
        correction_ids = tuple(correction.correction_id for correction in self.corrections)
        if len(correction_ids) != len(set(correction_ids)):
            raise ValueError("raw correction ids must be unique")
        claim_positions = tuple(claim.global_position for claim in self.claims)
        correction_positions = tuple(correction.global_position for correction in self.corrections)
        if claim_positions != tuple(sorted(claim_positions)):
            raise ValueError("raw claims must retain ledger order")
        if correction_positions != tuple(sorted(correction_positions)):
            raise ValueError("raw corrections must retain ledger order")
        claims_by_id = {claim.claim_id: claim for claim in self.claims}
        superseded: set[str] = set()
        for correction in self.corrections:
            replacement = claims_by_id.get(correction.replacement_claim_id)
            if replacement is None or replacement.correction_id != correction.correction_id:
                raise ValueError("raw correction replacement lineage is incomplete")
            if (
                replacement.global_position != correction.global_position
                or replacement.stream_sequence != correction.stream_sequence
            ):
                raise ValueError("raw correction replacement position is inconsistent")
            for target_id in correction.supersedes_claim_ids:
                target = claims_by_id.get(target_id)
                if target is None or target.global_position >= correction.global_position:
                    raise ValueError("raw corrections must target earlier claims")
                if target_id in superseded:
                    raise ValueError("raw corrections cannot supersede one claim twice")
                superseded.add(target_id)
        positions = (
            *(claim.stream_sequence for claim in self.claims),
            *(correction.stream_sequence for correction in self.corrections),
        )
        expected_sequence = max(positions, default=0)
        if self.last_source_stream_sequence != expected_sequence:
            raise ValueError("raw evidence does not match its source stream position")


class OperationalStateFold:
    """Pure scope-bound fold whose JSON state is safe to discard and rebuild."""

    version = 1

    def __init__(self, scope: OperationalStateScope) -> None:
        if not scope.bound:
            raise ValueError("operational-state folds require a bound scope")
        self.scope = scope
        self.name = f"operational-state-fold:{json_digest(_scope_payload(scope))}"

    def initial_state(self) -> RawOperationalState:
        return RawOperationalState(self.scope, (), (), 0)

    def apply(self, state: RawOperationalState, event: EventEnvelope) -> RawOperationalState:
        if state.scope != self.scope:
            raise ValueError("raw state belongs to a different operational-state fold")
        if event.global_position is None:
            raise ValueError("operational-state fold requires stored events")
        if event.event_type not in STATE_EVENT_TYPES or event.stream_id != self.scope.stream_id:
            return state
        if event_domain(event) != self.scope.domain:
            return state
        if event.stream_sequence <= state.last_source_stream_sequence:
            raise ValueError("operational-state fold events must advance the source stream")

        if event.event_type in OBSERVATION_EVENT_TYPES:
            additions = claims_from_event(event, self.scope.domain)
            _ensure_new_claim_ids(state.claims, additions)
            return RawOperationalState(
                self.scope,
                (*state.claims, *additions),
                state.corrections,
                event.stream_sequence,
            )

        correction, replacement = correction_from_event(event, self.scope.domain)
        claims_by_id = {claim.claim_id: claim for claim in state.claims}
        if correction.correction_id in {existing.correction_id for existing in state.corrections}:
            raise ValueError(
                f"correction id {correction.correction_id!r} was reused within the projection scope"
            )
        targets = tuple(claims_by_id.get(claim_id) for claim_id in correction.supersedes_claim_ids)
        missing = tuple(
            claim_id
            for claim_id, target in zip(
                correction.supersedes_claim_ids,
                targets,
                strict=True,
            )
            if target is None
        )
        if missing:
            rendered = ", ".join(repr(claim_id) for claim_id in missing)
            raise ValueError(f"correction targets must be earlier claims in scope: {rendered}")
        already_superseded = {
            claim_id for existing in state.corrections for claim_id in existing.supersedes_claim_ids
        }
        repeated = tuple(
            claim_id
            for claim_id in correction.supersedes_claim_ids
            if claim_id in already_superseded
        )
        if repeated:
            rendered = ", ".join(repr(claim_id) for claim_id in repeated)
            raise ValueError(f"correction targets were already superseded: {rendered}")
        resolved_targets = tuple(target for target in targets if target is not None)
        if any(target.key != replacement.key for target in resolved_targets):
            raise ValueError("a correction replacement must have the same fact key as its targets")
        if any(correction.effective_at < target.effective_at for target in resolved_targets):
            raise ValueError("a correction cannot take effect before a target claim")
        _ensure_new_claim_ids(state.claims, (replacement,))

        return RawOperationalState(
            self.scope,
            (*state.claims, replacement),
            (*state.corrections, correction),
            event.stream_sequence,
        )

    def dump_state(self, state: RawOperationalState) -> dict[str, JsonInput]:
        if state.scope != self.scope:
            raise ValueError("raw state belongs to a different operational-state fold")
        return {
            "schema_version": RAW_STATE_SCHEMA_VERSION,
            "scope": _scope_payload(state.scope),
            "claims": [_claim_state_payload(claim) for claim in state.claims],
            "corrections": [
                _correction_state_payload(correction) for correction in state.corrections
            ],
            "last_source_stream_sequence": state.last_source_stream_sequence,
        }

    def load_state(self, value: object) -> RawOperationalState:
        payload = _mapping(
            value,
            expected={
                "schema_version",
                "scope",
                "claims",
                "corrections",
                "last_source_stream_sequence",
            },
            label="raw operational state",
        )
        if payload["schema_version"] != RAW_STATE_SCHEMA_VERSION:
            raise ValueError("raw operational-state checkpoint schema is unsupported")
        scope = _scope_from_payload(payload["scope"])
        if scope != self.scope:
            raise ValueError("raw operational-state checkpoint belongs to a different scope")
        raw_claims = _sequence(payload["claims"], "raw claims")
        raw_corrections = _sequence(payload["corrections"], "raw corrections")
        last_sequence = _integer(
            payload["last_source_stream_sequence"],
            "last_source_stream_sequence",
        )
        return RawOperationalState(
            scope,
            tuple(_claim_from_state(item) for item in raw_claims),
            tuple(_correction_from_state(item) for item in raw_corrections),
            last_sequence,
        )

    def materialize(
        self,
        raw: RawOperationalState,
        *,
        cutoff_global_position: int,
        as_of_time: datetime | None = None,
    ) -> OperationalBeliefState:
        if raw.scope != self.scope:
            raise ValueError("raw state belongs to a different operational-state fold")
        _validate_effective_cutoff(as_of_time)
        if cutoff_global_position < 0:
            raise ValueError("cutoff_global_position must be non-negative")
        raw_positions = (
            *(claim.global_position for claim in raw.claims),
            *(correction.global_position for correction in raw.corrections),
        )
        if raw_positions and max(raw_positions) > cutoff_global_position:
            raise ValueError("raw evidence exceeds the requested ledger cutoff")

        eligible_claims = tuple(
            claim for claim in raw.claims if _effective_by(claim.effective_at, as_of_time)
        )
        applied_corrections = tuple(
            correction
            for correction in raw.corrections
            if _effective_by(correction.effective_at, as_of_time)
        )
        applied_superseded_ids = {
            claim_id
            for correction in applied_corrections
            for claim_id in correction.supersedes_claim_ids
        }
        candidates = tuple(
            claim for claim in eligible_claims if claim.claim_id not in applied_superseded_ids
        )
        selected = _select_candidates(candidates)
        selected_claims = tuple(
            claim
            for key in sorted(selected)
            for source in sorted(selected[key])
            for claim in sorted(selected[key][source], key=_claim_order)
        )
        current: list[BeliefClaim] = []
        expired: list[BeliefClaim] = []
        for claim in selected_claims:
            if (
                as_of_time is not None
                and claim.expires_at is not None
                and claim.expires_at <= as_of_time
            ):
                expired.append(claim)
                current.append(
                    replace(
                        claim,
                        value=None,
                        confidence=0.0,
                        epistemic_status=EpistemicStatus.UNKNOWN,
                        unknown_reason=UnknownReason.EXPIRED,
                    )
                )
            else:
                current.append(claim)
        claims = tuple(current)
        explicitly_superseded = tuple(
            sorted(
                (claim for claim in eligible_claims if claim.claim_id in applied_superseded_ids),
                key=_claim_order,
            )
        )
        return OperationalBeliefState(
            scope=self.scope,
            claims=claims,
            conflicts=_conflicts(claims),
            cutoff_global_position=cutoff_global_position,
            last_source_stream_sequence=raw.last_source_stream_sequence,
            superseded_claims=explicitly_superseded,
            applied_corrections=applied_corrections,
            effective_time_cutoff=as_of_time,
            expired_claims=tuple(expired),
        )


def event_domain(event: EventEnvelope) -> str:
    if "domain" not in event.payload:
        if event.event_type in CORRECTION_EVENT_TYPES:
            raise ValueError(f"correction event {event.event_id} requires a domain")
        observation_version = event.payload.get("observation_schema_version")
        if observation_version not in (None, "observation/v1"):
            raise ValueError(f"observation event {event.event_id} requires a domain")
        return LEGACY_OBSERVATION_DOMAIN
    value = event.payload["domain"]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"state event {event.event_id} requires a domain")
    return value


def claims_from_event(event: EventEnvelope, domain: str) -> tuple[BeliefClaim, ...]:
    raw_claims = event.payload.get("claims")
    if not isinstance(raw_claims, tuple):
        raise ValueError(f"observation event {event.event_id} requires a claims array")
    return tuple(
        _claim_from_event(event, domain, raw, index) for index, raw in enumerate(raw_claims)
    )


def _ensure_new_claim_ids(
    existing: Sequence[BeliefClaim],
    additions: Sequence[BeliefClaim],
) -> None:
    seen = {claim.claim_id for claim in existing}
    for claim in additions:
        if claim.claim_id in seen:
            raise ValueError(f"claim id {claim.claim_id!r} was reused within the projection scope")
        seen.add(claim.claim_id)


def correction_from_event(
    event: EventEnvelope,
    domain: str,
) -> tuple[BeliefCorrection, BeliefClaim]:
    version = event.payload.get("correction_schema_version")
    if version != CORRECTION_SCHEMA_VERSION:
        raise ValueError(f"correction event {event.event_id} has an unsupported schema")
    correction_id = _event_text(event.payload.get("correction_id"), "correction_id", event)
    reason = _event_text(event.payload.get("reason"), "reason", event)
    raw_supersedes = event.payload.get("supersedes_claim_ids")
    if not isinstance(raw_supersedes, tuple) or not raw_supersedes:
        raise ValueError(f"correction event {event.event_id} requires superseded claim ids")
    supersedes = tuple(_event_text(item, "superseded claim id", event) for item in raw_supersedes)
    if len(supersedes) != len(set(supersedes)):
        raise ValueError(f"correction event {event.event_id} repeats a superseded claim id")
    raw_evidence = event.payload.get("evidence")
    if not isinstance(raw_evidence, tuple) or not raw_evidence:
        raise ValueError(f"correction event {event.event_id} requires explicit evidence")
    if any(not isinstance(pointer, Mapping) for pointer in raw_evidence):
        raise ValueError(f"correction event {event.event_id} has invalid evidence")
    replacement = _claim_from_event(
        event,
        domain,
        event.payload.get("replacement"),
        0,
        correction_id=correction_id,
        supersedes_claim_ids=supersedes,
    )
    if replacement.claim_id in supersedes:
        raise ValueError("a correction replacement requires a new claim id")
    return (
        BeliefCorrection(
            correction_id=correction_id,
            supersedes_claim_ids=supersedes,
            replacement_claim_id=replacement.claim_id,
            reason=reason,
            effective_at=event.effective_at,
            recorded_at=event.recorded_at,
            source_event_id=event.event_id,
            source=event.source,
            actor=event.actor,
            correlation_id=event.correlation_id,
            domain=domain,
            stream_id=event.stream_id,
            stream_sequence=event.stream_sequence,
            global_position=_stored_position(event),
        ),
        replacement,
    )


def _claim_from_event(
    event: EventEnvelope,
    domain: str,
    raw: object,
    index: int,
    *,
    correction_id: str | None = None,
    supersedes_claim_ids: tuple[str, ...] = (),
) -> BeliefClaim:
    if not isinstance(raw, Mapping):
        raise ValueError(f"claim {index} in event {event.event_id} must be an object")
    raw_claim_id = raw.get("claim_id")
    claim_id = (
        f"{event.event_id}#claim:{index}"
        if raw_claim_id is None
        else _claim_text(raw_claim_id, "claim_id", event, index)
    )
    value = _scalar(raw.get("value"), f"claim {index} value")
    confidence_value = raw.get("confidence", 1.0)
    if isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float)):
        raise ValueError(f"claim {index} in event {event.event_id} has invalid confidence")
    expires_at = _optional_datetime(raw.get("expires_at"), "expires_at")
    return BeliefClaim(
        claim_id=claim_id,
        subject=_claim_text(raw.get("subject"), "subject", event, index),
        predicate=_claim_text(raw.get("predicate"), "predicate", event, index),
        value=value,
        confidence=float(confidence_value),
        effective_at=event.effective_at,
        recorded_at=event.recorded_at,
        source_event_id=event.event_id,
        source=event.source,
        actor=event.actor,
        correlation_id=event.correlation_id,
        domain=domain,
        stream_id=event.stream_id,
        stream_sequence=event.stream_sequence,
        global_position=_stored_position(event),
        correction_id=correction_id,
        supersedes_claim_ids=supersedes_claim_ids,
        expires_at=expires_at,
    )


def _select_candidates(
    claims: Sequence[BeliefClaim],
) -> dict[tuple[str, str], dict[str, list[BeliefClaim]]]:
    candidates: dict[tuple[str, str], dict[str, list[BeliefClaim]]] = {}
    for claim in claims:
        by_source = candidates.setdefault(claim.key, {})
        current = by_source.get(claim.source, [])
        if not current or claim.effective_at > current[0].effective_at:
            by_source[claim.source] = [claim]
        elif claim.effective_at == current[0].effective_at:
            current.append(claim)
    return candidates


def _conflicts(claims: Sequence[BeliefClaim]) -> tuple[BeliefConflict, ...]:
    grouped: dict[tuple[str, str], list[BeliefClaim]] = {}
    for claim in claims:
        if claim.epistemic_status is EpistemicStatus.OBSERVED:
            grouped.setdefault(claim.key, []).append(claim)
    conflicts: list[BeliefConflict] = []
    for key in sorted(grouped):
        group = sorted(grouped[key], key=_claim_order)
        if len({_value_key(claim) for claim in group}) <= 1:
            continue
        conflicts.append(
            BeliefConflict(
                subject=key[0],
                predicate=key[1],
                source_event_ids=tuple(claim.source_event_id for claim in group),
                claim_ids=tuple(claim.claim_id for claim in group),
                values=tuple(claim.value for claim in group),
            )
        )
    return tuple(conflicts)


def _scope_payload(scope: OperationalStateScope) -> dict[str, JsonInput]:
    return {"domain": scope.domain, "stream_id": scope.stream_id}


def _scope_from_payload(value: object) -> OperationalStateScope:
    payload = _mapping(value, expected={"domain", "stream_id"}, label="state scope")
    domain = _text(payload["domain"], "scope domain")
    stream_id = _optional_text(payload["stream_id"], "scope stream_id")
    return OperationalStateScope(domain, stream_id)


def _claim_state_payload(claim: BeliefClaim) -> dict[str, JsonInput]:
    return {
        "claim_id": claim.claim_id,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "value": claim.value,
        "confidence": claim.confidence,
        "effective_at": claim.effective_at.isoformat(),
        "recorded_at": claim.recorded_at.isoformat(),
        "source_event_id": claim.source_event_id,
        "source": claim.source,
        "actor": claim.actor,
        "correlation_id": claim.correlation_id,
        "domain": claim.domain,
        "stream_id": claim.stream_id,
        "stream_sequence": claim.stream_sequence,
        "global_position": claim.global_position,
        "correction_id": claim.correction_id,
        "supersedes_claim_ids": list(claim.supersedes_claim_ids),
        "expires_at": claim.expires_at.isoformat() if claim.expires_at is not None else None,
        "epistemic_status": claim.epistemic_status.value,
        "unknown_reason": claim.unknown_reason.value if claim.unknown_reason is not None else None,
    }


def _claim_from_state(value: object) -> BeliefClaim:
    payload = _mapping(
        value,
        expected={
            "claim_id",
            "subject",
            "predicate",
            "value",
            "confidence",
            "effective_at",
            "recorded_at",
            "source_event_id",
            "source",
            "actor",
            "correlation_id",
            "domain",
            "stream_id",
            "stream_sequence",
            "global_position",
            "correction_id",
            "supersedes_claim_ids",
            "expires_at",
            "epistemic_status",
            "unknown_reason",
        },
        label="raw claim",
    )
    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("raw claim confidence must be numeric")
    supersedes = tuple(
        _text(item, "superseded claim id")
        for item in _sequence(payload["supersedes_claim_ids"], "superseded claim ids")
    )
    status = EpistemicStatus(_text(payload["epistemic_status"], "epistemic_status"))
    unknown_value = payload["unknown_reason"]
    unknown_reason = (
        None if unknown_value is None else UnknownReason(_text(unknown_value, "unknown_reason"))
    )
    return BeliefClaim(
        claim_id=_text(payload["claim_id"], "claim_id"),
        subject=_text(payload["subject"], "subject"),
        predicate=_text(payload["predicate"], "predicate"),
        value=_scalar(payload["value"], "claim value"),
        confidence=float(confidence),
        effective_at=_datetime(payload["effective_at"], "effective_at"),
        recorded_at=_datetime(payload["recorded_at"], "recorded_at"),
        source_event_id=_text(payload["source_event_id"], "source_event_id"),
        source=_text(payload["source"], "source"),
        actor=_text(payload["actor"], "actor"),
        correlation_id=_text(payload["correlation_id"], "correlation_id"),
        domain=_text(payload["domain"], "domain"),
        stream_id=_text(payload["stream_id"], "stream_id"),
        stream_sequence=_integer(payload["stream_sequence"], "stream_sequence"),
        global_position=_integer(payload["global_position"], "global_position"),
        correction_id=_optional_text(payload["correction_id"], "correction_id"),
        supersedes_claim_ids=supersedes,
        expires_at=_optional_datetime(payload["expires_at"], "expires_at"),
        epistemic_status=status,
        unknown_reason=unknown_reason,
    )


def _correction_state_payload(correction: BeliefCorrection) -> dict[str, JsonInput]:
    return {
        "correction_id": correction.correction_id,
        "supersedes_claim_ids": list(correction.supersedes_claim_ids),
        "replacement_claim_id": correction.replacement_claim_id,
        "reason": correction.reason,
        "effective_at": correction.effective_at.isoformat(),
        "recorded_at": correction.recorded_at.isoformat(),
        "source_event_id": correction.source_event_id,
        "source": correction.source,
        "actor": correction.actor,
        "correlation_id": correction.correlation_id,
        "domain": correction.domain,
        "stream_id": correction.stream_id,
        "stream_sequence": correction.stream_sequence,
        "global_position": correction.global_position,
    }


def _correction_from_state(value: object) -> BeliefCorrection:
    payload = _mapping(
        value,
        expected={
            "correction_id",
            "supersedes_claim_ids",
            "replacement_claim_id",
            "reason",
            "effective_at",
            "recorded_at",
            "source_event_id",
            "source",
            "actor",
            "correlation_id",
            "domain",
            "stream_id",
            "stream_sequence",
            "global_position",
        },
        label="raw correction",
    )
    supersedes = tuple(
        _text(item, "superseded claim id")
        for item in _sequence(payload["supersedes_claim_ids"], "superseded claim ids")
    )
    return BeliefCorrection(
        correction_id=_text(payload["correction_id"], "correction_id"),
        supersedes_claim_ids=supersedes,
        replacement_claim_id=_text(payload["replacement_claim_id"], "replacement_claim_id"),
        reason=_text(payload["reason"], "reason"),
        effective_at=_datetime(payload["effective_at"], "effective_at"),
        recorded_at=_datetime(payload["recorded_at"], "recorded_at"),
        source_event_id=_text(payload["source_event_id"], "source_event_id"),
        source=_text(payload["source"], "source"),
        actor=_text(payload["actor"], "actor"),
        correlation_id=_text(payload["correlation_id"], "correlation_id"),
        domain=_text(payload["domain"], "domain"),
        stream_id=_text(payload["stream_id"], "stream_id"),
        stream_sequence=_integer(payload["stream_sequence"], "stream_sequence"),
        global_position=_integer(payload["global_position"], "global_position"),
    )


def _mapping(value: object, *, expected: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} field names must be strings")
    mapping = cast("Mapping[str, object]", value)
    keys = set(mapping)
    if keys != expected:
        raise ValueError(f"{label} fields do not match its schema")
    return mapping


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ValueError(f"{label} must be an array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _datetime(value: object, label: str) -> datetime:
    text = _text(value, label)
    try:
        result = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 datetime") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return result


def _optional_datetime(value: object, label: str) -> datetime | None:
    return None if value is None else _datetime(value, label)


def _scalar(value: object, label: str) -> JsonScalar:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError(f"{label} must be a JSON scalar")


def _claim_text(value: object, field: str, event: EventEnvelope, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"claim {index} in event {event.event_id} requires {field}")
    return value


def _event_text(value: object, field: str, event: EventEnvelope) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"correction event {event.event_id} requires {field}")
    return value


def _stored_position(event: EventEnvelope) -> int:
    position = event.global_position
    if position is None:
        raise ValueError("operational-state fold requires stored events")
    return position


def _validate_effective_cutoff(as_of_time: datetime | None) -> None:
    if as_of_time is not None and (as_of_time.tzinfo is None or as_of_time.utcoffset() is None):
        raise ValueError("as_of_time must be timezone-aware")


def _effective_by(effective_at: datetime, as_of_time: datetime | None) -> bool:
    return as_of_time is None or effective_at <= as_of_time


def _value_key(claim: BeliefClaim) -> str:
    return canonical_json({"value": claim.value})


def _claim_order(claim: BeliefClaim) -> tuple[datetime, datetime, int, str, str]:
    return (
        claim.effective_at,
        claim.recorded_at,
        claim.stream_sequence,
        claim.source_event_id,
        claim.claim_id,
    )
