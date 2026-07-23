from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from blackcell.kernel import JsonValue
from blackcell.orchestration.alpha_changes import ALPHA_CHANGE_PROPOSAL_OUTPUT_SCHEMA

ROOT = Path(__file__).parents[2]
EVIDENCE_PATH = ROOT / "release" / "alpha" / "live-provider-proof.json"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def test_live_provider_proof_is_redacted_terminal_and_source_checked() -> None:
    proof = cast("dict[str, Any]", json.loads(EVIDENCE_PATH.read_text(encoding="utf-8")))

    assert set(proof) == {
        "assurance",
        "cleanup",
        "disposition",
        "effects",
        "metrics",
        "project_registration",
        "provider",
        "recorded_on",
        "remediation",
        "replay",
        "schema_version",
        "scope",
        "security",
    }
    assert proof["schema_version"] == "blackcell-alpha-live-provider-proof/v1"
    assert proof["recorded_on"] == "2026-07-22"

    assurance = proof["assurance"]
    assert assurance == {
        "independent_agent_review": "waived-by-user-for-draft-pr",
        "mode": "root-authored",
        "release_claim": False,
    }

    disposition = proof["disposition"]
    assert disposition == {
        "alpha_tag_ready": False,
        "automatic_redispatch": False,
        "failure_code": "invalid-alpha-change-provider-proposal",
        "output_quality": "not-adjudicable",
        "proof_status": "terminal-provider-rejection",
        "successful_live_change": False,
    }
    assert proof["scope"]["provider_attempts"] == 1
    assert proof["scope"]["max_changed_files"] == 1
    assert proof["effects"] == {
        "acceptance_checks_executed": 0,
        "changed_paths": [],
        "proposal_artifact_stored": False,
        "repository_changes_applied": False,
    }

    metrics = proof["metrics"]
    assert metrics["durable_dispatch_to_terminal_ms"] == 6933
    assert metrics["external_billing_observable"] is False
    assert metrics["unavailable_reason"] == ("provider-result-rejected-before-metadata-artifact")
    for name in (
        "provider_cost_microusd",
        "provider_input_tokens",
        "provider_latency_ms",
        "provider_output_tokens",
    ):
        assert metrics[name] is None

    registration = proof["project_registration"]
    assert registration["kernform_check_status"] == "failed"
    assert registration["kernform_diagnostic_id"] == "KF-STATE-001"
    assert registration["managed_configuration_claim"] is False
    assert _DIGEST.fullmatch(registration["configuration_digest"])

    replay = proof["replay"]
    assert replay["artifact_integrity"] == "verified"
    assert replay["findings"] == []
    assert replay["processed_events"] == 9
    assert replay["restart_replay_verified"] is True
    assert {artifact["role"] for artifact in replay["artifacts"]} == {
        "context",
        "outcome",
    }
    assert all(artifact["verified"] is True for artifact in replay["artifacts"])
    for value in (
        replay["artifact_evidence_digest"],
        replay["state_digest"],
        replay["terminal_event_digest"],
        *(artifact["digest"] for artifact in replay["artifacts"]),
    ):
        assert _DIGEST.fullmatch(value)

    cleanup = proof["cleanup"]
    assert cleanup["pre_cleanup_worktree_clean"] is True
    assert cleanup["manual_cleanup_completed"] is True
    assert cleanup["deterministic_branch_deleted"] is True
    assert cleanup["registered_worktrees_after_cleanup"] == 1

    remediation = proof["remediation"]
    assert remediation["live_retry_performed"] is False
    assert remediation["output_schema_minimum_operations"] == 1
    for source in remediation["source_paths"]:
        path = ROOT / source
        assert path.is_file()
        assert path.resolve().is_relative_to(ROOT.resolve())

    properties = cast("Mapping[str, JsonValue]", ALPHA_CHANGE_PROPOSAL_OUTPUT_SCHEMA["properties"])
    operations = cast("Mapping[str, JsonValue]", properties["operations"])
    assert operations["minItems"] == 1

    assert proof["security"] == {
        "canonical_request_content_committed": False,
        "credential_values_committed": False,
        "provider_response_content_committed": False,
        "provider_tools_enabled": False,
    }
    encoded = EVIDENCE_PATH.read_text(encoding="utf-8")
    for forbidden in ("/home/", "/tmp/", "api-token", "OPENAI_API_KEY"):
        assert forbidden not in encoded
