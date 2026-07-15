from datetime import UTC, datetime

import msgspec
import pytest

from blackcell.interfaces.http import (
    ObservationIngestRequest,
    RunSubmissionRequest,
    WireContractError,
    decode_contract,
    encode_contract,
)


def test_run_submission_contract_round_trips_as_strict_versioned_json() -> None:
    contract = decode_contract(
        b"""{
          "schema_version": "run-submission-request/v1",
          "objective": "Inspect the repository",
          "approval_granted": false,
          "token_budget": 2000,
          "character_budget": 8000
        }""",
        RunSubmissionRequest,
    )

    assert contract.objective == "Inspect the repository"
    assert msgspec.json.decode(encode_contract(contract)) == {
        "schema_version": "run-submission-request/v1",
        "objective": "Inspect the repository",
        "approval_granted": False,
        "token_budget": 2000,
        "character_budget": 8000,
    }
    with pytest.raises(AttributeError):
        contract.objective = "mutated"  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "payload",
    (
        b'{"schema_version":"run-submission-request/v0","objective":"x"}',
        b'{"schema_version":"run-submission-request/v1","objective":"x","unknown":true}',
        b'{"schema_version":"run-submission-request/v1","objective":"","token_budget":1}',
        b'{"schema_version":"run-submission-request/v1","objective":"x","token_budget":0}',
        b'{"schema_version":"run-submission-request/v1","objective":"x","approval_granted":1}',
        b"",
    ),
)
def test_run_submission_contract_rejects_schema_drift_and_ambiguous_values(
    payload: bytes,
) -> None:
    with pytest.raises(WireContractError, match="invalid-request"):
        decode_contract(payload, RunSubmissionRequest)


def test_observation_contract_rejects_reserved_streams_and_naive_time() -> None:
    template = """{
      "schema_version": "observation-ingest-request/v1",
      "stream_id": "%s",
      "expected_sequence": 0,
      "source": "fixture/v1",
      "correlation_id": "correlation-1",
      "observations": [{
        "observation_id": "observation-1",
        "effective_at": "%s",
        "claims": [{
          "claim_id": "claim-1",
          "subject": "repository",
          "predicate": "git.clean",
          "value": true
        }],
        "evidence": [{"locator": "fixture://status"}]
      }]
    }"""

    decoded = decode_contract(
        (template % ("observation:fixture", "2026-07-13T12:00:00Z")).encode(),
        ObservationIngestRequest,
    )
    assert decoded.observations[0].effective_at == datetime(2026, 7, 13, 12, tzinfo=UTC)

    for stream_id, timestamp in (
        ("daily-operator-run:collision", "2026-07-13T12:00:00Z"),
        ("observation:fixture", "2026-07-13T12:00:00"),
    ):
        with pytest.raises(WireContractError, match="invalid-request"):
            decode_contract(
                (template % (stream_id, timestamp)).encode(),
                ObservationIngestRequest,
            )


def test_contract_decode_enforces_the_one_megabyte_body_bound() -> None:
    with pytest.raises(WireContractError, match="invalid-request"):
        decode_contract(b"{" + b"x" * 1_048_576, RunSubmissionRequest)
