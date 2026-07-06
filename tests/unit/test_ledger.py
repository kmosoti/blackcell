import json
import sqlite3
from pathlib import Path

from blackcell.cli.app import app
from blackcell.ledger import (
    init_ledger,
    list_events,
    list_runs,
    make_event,
    record_run,
    summarize_ledger,
)
from tests.cli_runner import CycloptsCliRunner


def test_init_ledger_creates_local_sqlite_schema(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"

    summary = init_ledger(path=db)

    assert db.exists()
    assert summary.path == db
    assert summary.schema_version == 1
    assert summary.run_count == 0
    assert summary.event_count == 0


def test_record_run_is_idempotent_and_lists_run_events(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    run_id = "run-local-1"
    event = make_event(
        run_id=run_id,
        sequence=0,
        kind="plan-step",
        source="harness",
        message="observe",
        payload={"step": "observe"},
    )

    first = record_run(
        path=db,
        run_id=run_id,
        kind="harness",
        status="ok",
        created_at="2026-07-06T00:00:00Z",
        payload={"runtime": "dry-run"},
        events=(event,),
    )
    second = record_run(
        path=db,
        run_id=run_id,
        kind="harness",
        status="ok",
        created_at="2026-07-06T00:00:00Z",
        payload={"runtime": "dry-run"},
        events=(event,),
    )

    assert first == second
    assert summarize_ledger(path=db).run_count == 1
    assert summarize_ledger(path=db).event_count == 1
    assert list_runs(path=db)[0].run_id == run_id
    assert list_events(path=db, run_id=run_id) == (event,)


def test_list_operations_do_not_create_missing_ledger(tmp_path: Path) -> None:
    db = tmp_path / "missing.sqlite3"

    assert list_runs(path=db) == ()
    assert list_events(path=db) == ()
    assert not db.exists()


def test_record_run_rejects_mismatched_event_run_id(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    event = make_event(
        run_id="other-run",
        sequence=0,
        kind="plan-step",
        source="harness",
        message="observe",
    )

    try:
        record_run(
            path=db,
            run_id="run-local-1",
            kind="harness",
            status="ok",
            created_at="2026-07-06T00:00:00Z",
            events=(event,),
        )
    except ValueError as error:
        assert "run_id must match" in str(error)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("record_run accepted a mismatched event run_id")


def test_record_run_rejects_divergent_run_replay(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"

    record_run(
        path=db,
        run_id="run-local-1",
        kind="harness",
        status="ok",
        created_at="2026-07-06T00:00:00Z",
    )

    try:
        record_run(
            path=db,
            run_id="run-local-1",
            kind="harness",
            status="changed",
            created_at="2026-07-06T00:00:00Z",
        )
    except ValueError as error:
        assert "ledger run conflict" in str(error)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("record_run accepted divergent run replay")


def test_record_run_rejects_divergent_event_sequence_replay(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    first = make_event(
        run_id="run-local-1",
        sequence=0,
        kind="plan-step",
        source="harness",
        message="observe",
    )
    changed = make_event(
        run_id="run-local-1",
        sequence=0,
        kind="plan-step",
        source="harness",
        message="changed",
    )

    record_run(
        path=db,
        run_id="run-local-1",
        kind="harness",
        status="ok",
        created_at="2026-07-06T00:00:00Z",
        events=(first,),
    )

    try:
        record_run(
            path=db,
            run_id="run-local-1",
            kind="harness",
            status="ok",
            created_at="2026-07-06T00:00:00Z",
            events=(changed,),
        )
    except ValueError as error:
        assert "ledger event conflict" in str(error)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("record_run accepted divergent event replay")


def test_sqlite_foreign_keys_are_enforced(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    init_ledger(path=db)

    with sqlite3.connect(db) as connection:
        connection.execute("pragma foreign_keys = on")
        try:
            connection.execute(
                """
                insert into events(event_id, run_id, sequence, kind, source, message, payload_json)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                ("event-1", "missing-run", 0, "kind", "source", "message", "{}"),
            )
        except sqlite3.IntegrityError as error:
            assert "FOREIGN KEY" in str(error).upper()
        else:  # pragma: no cover - assertion branch
            raise AssertionError("SQLite accepted event without matching run")


def test_ledger_cli_init_runs_events_json(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    runner = CycloptsCliRunner()
    init_result = runner.invoke(app, ["ledger", "init", "--db", str(db)])

    assert init_result.exit_code == 0
    init_payload = json.loads(init_result.stdout)
    assert init_payload["run_count"] == 0
    assert init_payload["event_count"] == 0

    record_run(
        path=db,
        run_id="run-local-1",
        kind="harness",
        status="ok",
        created_at="2026-07-06T00:00:00Z",
        events=(
            make_event(
                run_id="run-local-1",
                sequence=0,
                kind="plan-step",
                source="harness",
                message="observe",
            ),
        ),
    )

    runs_result = runner.invoke(app, ["ledger", "runs", "--db", str(db)])
    assert runs_result.exit_code == 0
    runs_payload = json.loads(runs_result.stdout)
    assert runs_payload["runs"][0]["run_id"] == "run-local-1"

    events_result = runner.invoke(
        app,
        ["ledger", "events", "--db", str(db), "--run", "run-local-1"],
    )
    assert events_result.exit_code == 0
    events_payload = json.loads(events_result.stdout)
    assert events_payload["events"][0]["message"] == "observe"
