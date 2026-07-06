import hashlib

from blackcell.latent.ids import stable_digest
from blackcell.latent.models import FeatureMap, LatentState
from blackcell.world.models import Fact, WorldSnapshot

ENCODER_VERSION = "latent-v0-deterministic"


def encode_world_state(
    snapshot: WorldSnapshot,
    *,
    source: str = "world.observe",
    policy: FeatureMap | None = None,
) -> LatentState:
    """Encode world facts into an inspectable deterministic latent capsule."""

    fact_tokens = tuple(_fact_token(fact) for fact in snapshot.facts)
    semantic = _semantic_sketch(fact_tokens)
    structural: FeatureMap = {
        "observation_count": len(snapshot.observations),
        "fact_count": len(snapshot.facts),
        "belief_count": len(snapshot.beliefs),
        "expectation_count": len(snapshot.expectations),
        "surprise_count": len(snapshot.surprises),
        "has_docs": _has_fact(snapshot.facts, "repo", "has_path", "docs"),
        "has_tests": _has_fact(snapshot.facts, "repo", "has_path", "tests"),
        "workspace_dirty": _has_fact(snapshot.facts, "repo", "workspace_state", "dirty"),
    }
    telemetry: FeatureMap = {
        "runtime_adapter_count": sum(
            1
            for fact in snapshot.facts
            if fact.subject == "runtime" and fact.predicate == "has_adapter"
        ),
        "check_count": 0,
        "retry_count": 0,
    }
    policy_features: FeatureMap = {
        "runtime": "dry-run",
        "sandbox": "local",
        "training_enabled": False,
    }
    if policy:
        policy_features.update(policy)
    symbolic: FeatureMap = {
        "nesy_valid": True,
        "expectation_count": len(snapshot.expectations),
        "surprise_count": len(snapshot.surprises),
    }
    state_payload = {
        "source": source,
        "semantic": semantic,
        "structural": structural,
        "telemetry": telemetry,
        "policy": policy_features,
        "symbolic": symbolic,
        "encoder_version": ENCODER_VERSION,
    }
    return LatentState(
        state_id=stable_digest("latent-state", state_payload),
        source=source,
        semantic=semantic,
        structural=structural,
        telemetry=telemetry,
        policy=policy_features,
        symbolic=symbolic,
        encoder_version=ENCODER_VERSION,
    )


def _fact_token(fact: Fact) -> str:
    return f"{fact.subject}:{fact.predicate}:{fact.object}:{fact.source}"


def _has_fact(facts: tuple[Fact, ...], subject: str, predicate: str, object_: str) -> bool:
    return any(
        fact.subject == subject and fact.predicate == predicate and fact.object == object_
        for fact in facts
    )


def _semantic_sketch(tokens: tuple[str, ...], *, width: int = 8) -> tuple[float, ...]:
    buckets = [0.0] * width
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = digest[0] % width
        sign = 1.0 if digest[1] % 2 == 0 else -1.0
        buckets[bucket] += sign
    total = sum(abs(value) for value in buckets) or 1.0
    return tuple(round(value / total, 6) for value in buckets)
