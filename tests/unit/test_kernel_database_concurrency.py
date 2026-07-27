from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

import blackcell.kernel.database as kernel_database
from blackcell.kernel import ArtifactStore, EventStore, SchemaVersionError
from blackcell.kernel.database import SCHEMA_VERSION, connect, initialize_database


@pytest.mark.parametrize("round_number", range(4))
def test_concurrent_first_open_applies_kernel_migration_once(
    tmp_path: Path,
    round_number: int,
) -> None:
    database = tmp_path / f"round-{round_number}" / "kernel.sqlite3"
    callers = 16
    barrier = Barrier(callers)

    def initialize(index: int) -> None:
        barrier.wait()
        if index % 2:
            EventStore(database)
        else:
            ArtifactStore(
                tmp_path / f"round-{round_number}" / f"artifacts-{index}",
                database_path=database,
            )

    with ThreadPoolExecutor(max_workers=callers) as executor:
        tuple(executor.map(initialize, range(callers)))

    # A later open remains an exact no-op for the migration ledger.
    initialize_database(database)
    with connect(database) as connection:
        assert connection.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("pragma foreign_keys").fetchone()[0] == 1
        assert tuple(
            int(row["version"])
            for row in connection.execute(
                "select version from kernel_schema_migrations order by version"
            ).fetchall()
        ) == (SCHEMA_VERSION,)
        tables = {
            str(row[0])
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            ).fetchall()
        }
    assert {
        "event_streams",
        "kernel_artifacts",
        "kernel_events",
        "kernel_schema_migrations",
        "projection_checkpoints",
    } <= tables


def test_failed_kernel_migration_rolls_back_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "kernel.sqlite3"
    broken_schema = """
    create table migration_probe (value integer not null);
    insert into missing_migration_table(value) values (1);
    """

    with monkeypatch.context() as patch:
        patch.setattr(kernel_database, "_INITIAL_SCHEMA", broken_schema)
        with pytest.raises(sqlite3.OperationalError, match="missing_migration_table"):
            initialize_database(database)

    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("pragma user_version").fetchone()[0] == 0
        assert (
            connection.execute(
                "select name from sqlite_master where name = 'migration_probe'"
            ).fetchone()
            is None
        )

    initialize_database(database)
    with connect(database) as connection:
        assert connection.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            connection.execute("select version from kernel_schema_migrations").fetchone()[0]
            == SCHEMA_VERSION
        )


def test_existing_incompatible_database_is_rejected_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "kernel.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("create table former_runtime_state (value text not null)")
        connection.execute("insert into former_runtime_state(value) values ('preserve-me')")
        connection.execute("pragma user_version = 1")

    before = database.read_bytes()

    with pytest.raises(SchemaVersionError, match="incompatible"):
        initialize_database(database)

    assert database.read_bytes() == before
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("pragma user_version").fetchone()[0] == 1
        assert connection.execute("select value from former_runtime_state").fetchone()[0] == (
            "preserve-me"
        )
