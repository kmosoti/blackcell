from __future__ import annotations

import msgspec
import pytest

from blackcell.interfaces.http import (
    AlphaAcceptanceCheck,
    AlphaCancelRunRequest,
    AlphaEventResponse,
    AlphaNodeBudget,
    AlphaPlanNode,
    AlphaPlanRequest,
    AlphaProjectRequest,
    WireContractError,
    alpha_plan_topological_order,
    decode_contract,
)
from blackcell.orchestration.alpha_acceptance import MAX_ALPHA_ACCEPTANCE_TIMEOUT_SECONDS

_DIGEST = "sha256:" + ("a" * 64)
_BASE_COMMIT = "b" * 40


def test_alpha_contracts_are_closed_versioned_and_bounded() -> None:
    request = decode_contract(
        msgspec.json.encode(
            {
                "schema_version": "alpha-project-request/v1",
                "project_id": "project-1",
                "root": "/tmp/project",
                "configuration_provider": "kernform",
                "configuration_version": "0.1.0",
                "configuration_digest": _DIGEST,
                "idempotency_key": "project-1",
            }
        ),
        AlphaProjectRequest,
    )
    assert request.project_id == "project-1"

    for invalid in (
        {
            "schema_version": "alpha-project-request/v2",
            "project_id": "project-1",
            "root": "/tmp/project",
            "configuration_provider": "kernform",
            "configuration_version": "0.1.0",
            "configuration_digest": _DIGEST,
            "idempotency_key": "project-1",
        },
        {
            "schema_version": "alpha-project-request/v1",
            "project_id": "project-1",
            "root": "/tmp/project",
            "configuration_provider": "kernform",
            "configuration_version": "0.1.0",
            "configuration_digest": _DIGEST,
            "idempotency_key": "project-1",
            "unknown": True,
        },
        {
            "schema_version": "alpha-project-request/v1",
            "project_id": "project:invalid",
            "root": "/tmp/project",
            "configuration_provider": "kernform",
            "configuration_version": "0.1.0",
            "configuration_digest": "not-a-digest",
            "idempotency_key": "project-1",
        },
    ):
        with pytest.raises(WireContractError):
            decode_contract(msgspec.json.encode(invalid), AlphaProjectRequest)

    plan = _valid_plan()
    assert alpha_plan_topological_order(plan.nodes) == ("inspect", "verify")


def test_alpha_plan_rejects_cycles_undeclared_effects_and_untestable_nodes() -> None:
    valid = msgspec.to_builtins(_valid_plan())
    assert isinstance(valid, dict)

    cyclic = {**valid, "nodes": [dict(node) for node in valid["nodes"]]}
    cyclic["nodes"][0]["depends_on"] = ["verify"]
    with pytest.raises(WireContractError):
        decode_contract(msgspec.json.encode(cyclic), AlphaPlanRequest)

    undeclared = {**valid, "nodes": [dict(node) for node in valid["nodes"]]}
    undeclared["nodes"][1]["effects"] = ["network"]
    with pytest.raises(WireContractError):
        decode_contract(msgspec.json.encode(undeclared), AlphaPlanRequest)

    untestable = {**valid, "nodes": [dict(node) for node in valid["nodes"]]}
    untestable["nodes"][0]["checks"] = []
    with pytest.raises(WireContractError):
        decode_contract(msgspec.json.encode(untestable), AlphaPlanRequest)

    unbounded_write = {**valid, "nodes": [dict(node) for node in valid["nodes"]]}
    unbounded_write["allowed_effects"] = ["repository-write", "process"]
    unbounded_write["nodes"][0]["effects"] = ["repository-write"]
    with pytest.raises(WireContractError):
        decode_contract(msgspec.json.encode(unbounded_write), AlphaPlanRequest)


def test_alpha_cancel_contract_is_closed_versioned_and_bounded() -> None:
    valid = {
        "schema_version": "alpha-cancel-run-request/v1",
        "idempotency_key": "cancel-run-1",
    }
    decoded = decode_contract(msgspec.json.encode(valid), AlphaCancelRunRequest)
    assert decoded.idempotency_key == "cancel-run-1"

    for invalid in (
        {**valid, "schema_version": "alpha-cancel-run-request/v2"},
        {**valid, "unexpected": True},
        {**valid, "idempotency_key": "invalid:key"},
    ):
        with pytest.raises(WireContractError):
            decode_contract(msgspec.json.encode(invalid), AlphaCancelRunRequest)


def test_alpha_event_contract_accepts_provider_dispatch_marker() -> None:
    event = decode_contract(
        msgspec.json.encode(
            {
                "schema_version": "alpha-event/v1",
                "event_id": "event-1",
                "cursor": 1,
                "stream_id": "alpha:run:run-1",
                "stream_sequence": 4,
                "event_type": "alpha.node.provider-dispatch-started",
                "event_schema_version": 1,
                "recorded_at": "2026-07-22T18:00:00+00:00",
                "correlation_id": "correlation-1",
                "causation_id": "prepared-event",
                "actor": "worker-1",
                "payload_digest": _DIGEST,
                "payload": {
                    "provider_request_id": "alpha-change-request",
                    "context_digest": _DIGEST,
                },
            }
        ),
        AlphaEventResponse,
    )
    assert event.event_type == "alpha.node.provider-dispatch-started"


def test_alpha_event_contract_accepts_worktree_cleanup_events() -> None:
    for sequence, event_type in enumerate(
        (
            "alpha.node.worktree-cleanup-requested",
            "alpha.node.worktree-cleaned",
            "alpha.node.worktree-cleanup-failed",
        ),
        start=1,
    ):
        event = decode_contract(
            msgspec.json.encode(
                {
                    "schema_version": "alpha-event/v1",
                    "event_id": f"event-{sequence}",
                    "cursor": sequence,
                    "stream_id": "alpha:run:run-1",
                    "stream_sequence": sequence,
                    "event_type": event_type,
                    "event_schema_version": 1,
                    "recorded_at": "2026-07-22T18:00:00+00:00",
                    "correlation_id": "correlation-1",
                    "causation_id": "success-event",
                    "actor": "retention-worker",
                    "payload_digest": _DIGEST,
                    "payload": {"status": event_type.removeprefix("alpha.node.")},
                }
            ),
            AlphaEventResponse,
        )
        assert event.event_type == event_type


def test_alpha_event_contract_accepts_review_and_verification_events() -> None:
    event_types = (
        "alpha.review.claimed",
        "alpha.review.lease-renewed",
        "alpha.review.provider-dispatch-started",
        "alpha.review.succeeded",
        "alpha.review.failed",
        "alpha.review.requeued",
        "alpha.review.reconciliation-required",
        "alpha.verification.claimed",
        "alpha.verification.completed",
        "alpha.verification.failed",
        "alpha.verification.requeued",
    )
    for sequence, event_type in enumerate(event_types, start=1):
        event = decode_contract(
            msgspec.json.encode(
                {
                    "schema_version": "alpha-event/v1",
                    "event_id": f"event-{sequence}",
                    "cursor": sequence,
                    "stream_id": "alpha:review:run-1",
                    "stream_sequence": sequence,
                    "event_type": event_type,
                    "event_schema_version": 1,
                    "recorded_at": "2026-07-22T18:00:00+00:00",
                    "correlation_id": "correlation-1",
                    "causation_id": "source-event",
                    "actor": "independent-worker",
                    "payload_digest": _DIGEST,
                    "payload": {"status": "bounded"},
                }
            ),
            AlphaEventResponse,
        )
        assert event.event_type == event_type


def test_alpha_plan_requires_positive_input_only_for_repository_writers() -> None:
    check = AlphaAcceptanceCheck("check-1", ("python", "--version"))
    acceptance_only = AlphaPlanNode(
        node_id="accept",
        objective="Run acceptance without requesting a model change.",
        depends_on=(),
        budget=AlphaNodeBudget(0, 0, 30, 0, 0),
        effects=("repository-read", "process"),
        allowed_paths=(),
        checks=(check,),
    )

    assert acceptance_only.budget.max_input_tokens == 0
    with pytest.raises(WireContractError):
        AlphaPlanNode(
            node_id="write",
            objective="Request a bounded repository change.",
            depends_on=(),
            budget=AlphaNodeBudget(0, 1_000, 30, 0, 1),
            effects=("repository-read", "repository-write", "process"),
            allowed_paths=("src/example.py",),
            checks=(check,),
        )


def test_alpha_plan_requires_explicit_check_effects_and_serial_writers() -> None:
    valid = msgspec.to_builtins(_valid_plan())
    assert isinstance(valid, dict)
    missing_process = {**valid, "nodes": [dict(node) for node in valid["nodes"]]}
    missing_process["nodes"][0]["effects"] = ["repository-read"]
    with pytest.raises(WireContractError):
        decode_contract(msgspec.json.encode(missing_process), AlphaPlanRequest)

    writer_budget = AlphaNodeBudget(1_000, 1_000, 30, 0, 1)
    writer = AlphaPlanNode(
        node_id="write-a",
        objective="Apply the first bounded change.",
        depends_on=(),
        budget=writer_budget,
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src/a.py",),
        checks=(AlphaAcceptanceCheck("write-a-check", ("python", "-m", "compileall", "src")),),
    )
    parallel = AlphaPlanNode(
        node_id="write-b",
        objective="Apply the second bounded change.",
        depends_on=(),
        budget=writer_budget,
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src/b.py",),
        checks=(AlphaAcceptanceCheck("write-b-check", ("python", "-m", "compileall", "src")),),
    )
    with pytest.raises(WireContractError):
        AlphaPlanRequest(
            schema_version="alpha-plan-request/v1",
            plan_id="parallel-writers",
            project_id="project-1",
            intent_id="intent-1",
            base_commit=_BASE_COMMIT,
            allowed_effects=("repository-read", "repository-write", "process"),
            nodes=(writer, parallel),
            idempotency_key="parallel-writers",
        )

    serial = msgspec.structs.replace(parallel, depends_on=(writer.node_id,))
    accepted = AlphaPlanRequest(
        schema_version="alpha-plan-request/v1",
        plan_id="serial-writers",
        project_id="project-1",
        intent_id="intent-1",
        base_commit=_BASE_COMMIT,
        allowed_effects=("repository-read", "repository-write", "process"),
        nodes=(writer, serial),
        idempotency_key="serial-writers",
    )
    assert alpha_plan_topological_order(accepted.nodes) == ("write-a", "write-b")


def test_alpha_node_budget_timeout_matches_executable_acceptance_limit() -> None:
    accepted = AlphaNodeBudget(1_000, 1_000, MAX_ALPHA_ACCEPTANCE_TIMEOUT_SECONDS, 0, 0)

    assert accepted.timeout_seconds == 600
    with pytest.raises(WireContractError):
        AlphaNodeBudget(1_000, 1_000, MAX_ALPHA_ACCEPTANCE_TIMEOUT_SECONDS + 1, 0, 0)


def test_alpha_check_identity_and_alias_match_executable_acceptance_contract() -> None:
    boundary = AlphaAcceptanceCheck("check-1", ("a" * 64, "--version"))

    assert boundary.argv[0] == "a" * 64
    for check_id in ("-check", "a" * 121):
        with pytest.raises(WireContractError):
            AlphaAcceptanceCheck(check_id, ("python", "--version"))
    for executable in ("tool/name", "a" * 65):
        with pytest.raises(WireContractError):
            AlphaAcceptanceCheck("check-1", (executable, "--version"))


def test_alpha_plan_paths_match_the_downstream_git_metadata_prohibition() -> None:
    budget = AlphaNodeBudget(1_000, 1_000, 30, 0, 1)

    for repository_path in (".git", "src/.git", "src/.git/config"):
        with pytest.raises(WireContractError):
            AlphaPlanNode(
                node_id="write",
                objective="Apply one bounded change.",
                depends_on=(),
                budget=budget,
                effects=("repository-read", "repository-write", "process"),
                allowed_paths=(repository_path,),
                checks=(AlphaAcceptanceCheck("write-check", ("python", "--version")),),
            )

    accepted = AlphaPlanNode(
        node_id="write",
        objective="Apply one bounded change.",
        depends_on=(),
        budget=budget,
        effects=("repository-read", "repository-write", "process"),
        allowed_paths=("src/.github/config.yml",),
        checks=(AlphaAcceptanceCheck("write-check", ("python", "--version")),),
    )
    assert accepted.allowed_paths == ("src/.github/config.yml",)


def _valid_plan() -> AlphaPlanRequest:
    budget = AlphaNodeBudget(
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        timeout_seconds=30,
        max_cost_microusd=0,
        max_changed_files=0,
    )
    return AlphaPlanRequest(
        schema_version="alpha-plan-request/v1",
        plan_id="plan-1",
        project_id="project-1",
        intent_id="intent-1",
        base_commit=_BASE_COMMIT,
        allowed_effects=("repository-read", "process"),
        nodes=(
            AlphaPlanNode(
                node_id="inspect",
                objective="Inspect the bounded project surface.",
                depends_on=(),
                budget=budget,
                effects=("repository-read", "process"),
                allowed_paths=(),
                checks=(
                    AlphaAcceptanceCheck(
                        check_id="inspect-pass",
                        argv=("python", "-m", "compileall", "src"),
                    ),
                ),
            ),
            AlphaPlanNode(
                node_id="verify",
                objective="Verify the declared outcome.",
                depends_on=("inspect",),
                budget=budget,
                effects=("repository-read", "process"),
                allowed_paths=(),
                checks=(
                    AlphaAcceptanceCheck(
                        check_id="verify-pass",
                        argv=("pytest", "tests/unit/test_example.py", "-q"),
                    ),
                ),
            ),
        ),
        idempotency_key="plan-1",
    )
