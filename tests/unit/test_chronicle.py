"""SQLite chronicle immutability and credential rejection."""

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from blackcell.contracts.errors import PolicyFailure
from blackcell.ledger.sqlite import Chronicle, EventType


def test_chronicle_appends_and_filters_events(tmp_path: Path) -> None:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")
    first_id = chronicle.append(
        EventType.DIRECTIVE_VALIDATED,
        "BCP-0001",
        {"digest": "sha256:abc"},
    )
    chronicle.append(EventType.ANOMALY_DETECTED, "BCP-0002", {"code": "test"})

    events = chronicle.events("BCP-0001")

    assert first_id == 1
    assert len(events) == 1
    assert events[0].event_type == "directive_validated"
    assert events[0].payload == {"digest": "sha256:abc"}


def test_database_triggers_reject_update_and_delete(tmp_path: Path) -> None:
    path = tmp_path / "chronicle.sqlite3"
    chronicle = Chronicle(path)
    chronicle.append(EventType.DIRECTIVE_VALIDATED, "BCP-0001")

    connection = sqlite3.connect(path)
    with connection, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE events SET plan_id = 'BCP-9999' WHERE id = 1")
    with connection, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM events WHERE id = 1")
    connection.close()


def test_anomaly_resolution_is_append_only_and_unblocks_future_work(tmp_path: Path) -> None:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")
    anomaly_id = chronicle.append(
        EventType.ANOMALY_DETECTED,
        "BCP-0001",
        {"code": "conflict"},
    )

    resolution_id = chronicle.resolve_anomaly(anomaly_id, "Verified provider normalization.")

    assert resolution_id > anomaly_id
    assert chronicle.unresolved_anomalies("BCP-0001") == []
    resolution = chronicle.events("BCP-0001")[-1]
    assert resolution.event_type == "anomaly_resolved"
    assert resolution.payload == {
        "anomaly_id": anomaly_id,
        "note": "Verified provider normalization.",
    }


def test_anomaly_cannot_be_resolved_twice(tmp_path: Path) -> None:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")
    anomaly_id = chronicle.append(EventType.ANOMALY_DETECTED, "BCP-0001")
    chronicle.resolve_anomaly(anomaly_id, "Reviewed.")

    with pytest.raises(PolicyFailure, match="already resolved"):
        chronicle.resolve_anomaly(anomaly_id, "Reviewed again.")


@pytest.mark.parametrize(
    "payload",
    [
        {"authorization": "Bearer redacted"},
        {"nested": {"LINEAR_API_KEY": "redacted"}},
        {"items": [{"github_token": "redacted"}]},
    ],
)
def test_chronicle_rejects_credential_fields(tmp_path: Path, payload: Mapping[str, Any]) -> None:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")

    with pytest.raises(PolicyFailure, match="forbidden credential field"):
        chronicle.append(EventType.DIRECTIVE_VALIDATED, "BCP-0001", payload)


def test_chronicle_rejects_credential_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chronicle = Chronicle(tmp_path / "chronicle.sqlite3")
    monkeypatch.setenv("LINEAR_API_KEY", "linear-secret-for-test")

    with pytest.raises(PolicyFailure, match="credential material"):
        chronicle.append(
            EventType.DIRECTIVE_VALIDATED,
            "BCP-0001",
            {"message": "prefix linear-secret-for-test suffix"},
        )
