import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from blackcell.latent.models import LatentSimulation, LatentTransition

SCHEMA_VERSION = 1
DEFAULT_LEDGER_PATH = Path(".blackcell") / "latent.sqlite3"


@dataclass(frozen=True, slots=True)
class LatentLedgerSummary:
    path: Path
    schema_version: int
    state_count: int
    prediction_count: int
    error_count: int
    transition_count: int
    sample_count: int


@dataclass(frozen=True, slots=True)
class LatentLedgerRecordResult:
    path: Path
    state_id: str
    prediction_id: str
    actual_state_id: str
    error_id: str
    transition_id: str
    sample_id: str


@dataclass(frozen=True, slots=True)
class LatentActionStats:
    action_id: str
    sample_count: int
    mean_semantic_distance: float
    surprise_count: int
    confidence_label: str


@dataclass(frozen=True, slots=True)
class LatentLedgerStats:
    path: Path
    action_stats: tuple[LatentActionStats, ...]


def record_simulation(
    simulation: LatentSimulation,
    *,
    path: Path = DEFAULT_LEDGER_PATH,
) -> LatentLedgerRecordResult:
    with _connect(path) as connection:
        _ensure_schema(connection)
        _insert_json(
            connection,
            "latent_states",
            "state_id",
            simulation.state.state_id,
            simulation.state,
        )
        _insert_json(
            connection,
            "latent_states",
            "state_id",
            simulation.prediction.predicted_state.state_id,
            simulation.prediction.predicted_state,
        )
        _insert_json(
            connection,
            "latent_states",
            "state_id",
            simulation.actual_state.state_id,
            simulation.actual_state,
        )
        _insert_json(
            connection,
            "latent_predictions",
            "prediction_id",
            simulation.prediction.prediction_id,
            simulation.prediction,
        )
        _insert_json(
            connection,
            "latent_errors",
            "error_id",
            simulation.error.error_id,
            simulation.error,
        )
        _insert_json(
            connection,
            "latent_transitions",
            "transition_id",
            simulation.transition.transition_id,
            simulation.transition,
        )
        _insert_json(
            connection,
            "self_supervision_samples",
            "sample_id",
            simulation.self_supervision_sample.sample_id,
            simulation.self_supervision_sample,
        )
    return LatentLedgerRecordResult(
        path=path,
        state_id=simulation.state.state_id,
        prediction_id=simulation.prediction.prediction_id,
        actual_state_id=simulation.actual_state.state_id,
        error_id=simulation.error.error_id,
        transition_id=simulation.transition.transition_id,
        sample_id=simulation.self_supervision_sample.sample_id,
    )


def summarize_ledger(path: Path = DEFAULT_LEDGER_PATH) -> LatentLedgerSummary:
    with _connect(path) as connection:
        _ensure_schema(connection)
        return LatentLedgerSummary(
            path=path,
            schema_version=SCHEMA_VERSION,
            state_count=_count(connection, "latent_states"),
            prediction_count=_count(connection, "latent_predictions"),
            error_count=_count(connection, "latent_errors"),
            transition_count=_count(connection, "latent_transitions"),
            sample_count=_count(connection, "self_supervision_samples"),
        )


def load_transitions(path: Path = DEFAULT_LEDGER_PATH) -> tuple[LatentTransition, ...]:
    if not path.exists():
        return ()
    try:
        with _connect_readonly(path) as connection:
            rows = connection.execute(
                "select payload_json from latent_transitions order by transition_id"
            ).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return ()
        raise
    return tuple(_transition_from_json(row[0]) for row in rows)


def summarize_prediction_stats(path: Path = DEFAULT_LEDGER_PATH) -> LatentLedgerStats:
    if not path.exists():
        return LatentLedgerStats(path=path, action_stats=())
    try:
        with _connect_readonly(path) as connection:
            prediction_rows = connection.execute(
                "select payload_json from latent_predictions"
            ).fetchall()
            error_rows = connection.execute("select payload_json from latent_errors").fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return LatentLedgerStats(path=path, action_stats=())
        raise

    action_by_prediction = {
        _required_str(prediction, "prediction_id"): _action_id(prediction)
        for prediction in (_loads_object(row[0]) for row in prediction_rows)
    }
    distances_by_action: dict[str, list[float]] = {}
    surprises_by_action: dict[str, int] = {}
    for error in (_loads_object(row[0]) for row in error_rows):
        prediction_id = _required_str(error, "prediction_id")
        action_id = action_by_prediction.get(prediction_id)
        if action_id is None:
            continue
        distances_by_action.setdefault(action_id, []).append(
            _required_number(error, "semantic_distance")
        )
        if _required_str(error, "surprise") != "none":
            surprises_by_action[action_id] = surprises_by_action.get(action_id, 0) + 1

    action_ids = sorted(set(action_by_prediction.values()) | set(distances_by_action))
    return LatentLedgerStats(
        path=path,
        action_stats=tuple(
            LatentActionStats(
                action_id=action_id,
                sample_count=len(distances_by_action.get(action_id, ())),
                mean_semantic_distance=_mean(distances_by_action.get(action_id, ())),
                surprise_count=surprises_by_action.get(action_id, 0),
                confidence_label=_confidence_label(len(distances_by_action.get(action_id, ()))),
            )
            for action_id in action_ids
        ),
    )


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


@contextmanager
def _connect_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        yield connection
    finally:
        connection.close()


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        create table if not exists latent_meta (
            key text primary key,
            value text not null
        );
        create table if not exists latent_states (
            state_id text primary key,
            payload_json text not null
        );
        create table if not exists latent_predictions (
            prediction_id text primary key,
            payload_json text not null
        );
        create table if not exists latent_errors (
            error_id text primary key,
            payload_json text not null
        );
        create table if not exists latent_transitions (
            transition_id text primary key,
            payload_json text not null
        );
        create table if not exists self_supervision_samples (
            sample_id text primary key,
            payload_json text not null
        );
        """
    )
    connection.execute(
        "insert or replace into latent_meta(key, value) values (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )


def _insert_json(
    connection: sqlite3.Connection,
    table: str,
    key_column: str,
    key: str,
    value: object,
) -> None:
    connection.execute(
        f"insert or replace into {table}({key_column}, payload_json) values (?, ?)",
        (key, json.dumps(_jsonable(value), sort_keys=True)),
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f"select count(*) from {table}").fetchone()
    value = row[0]
    if not isinstance(value, int):
        raise TypeError(f"unexpected count value for {table}: {value!r}")
    return value


def _transition_from_json(payload: str) -> LatentTransition:
    data = _loads_object(payload)
    return LatentTransition(
        transition_id=_required_str(data, "transition_id"),
        from_state_id=_required_str(data, "from_state_id"),
        action_id=_required_str(data, "action_id"),
        predicted_state_id=_required_str(data, "predicted_state_id"),
        actual_state_id=_required_str(data, "actual_state_id"),
        error_id=_required_str(data, "error_id"),
        outcome=_required_str(data, "outcome"),
        evidence_run_id=_optional_str(data, "evidence_run_id"),
        evidence_event_ids=_optional_str_tuple(data, "evidence_event_ids"),
    )


def _required_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise TypeError(f"latent transition field {key!r} must be a string")
    return value


def _optional_str(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"latent transition field {key!r} must be a string")
    return value


def _optional_str_tuple(data: dict[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError(f"latent transition field {key!r} must be a list")
    result = tuple(str(item) for item in value if isinstance(item, str))
    if len(result) != len(value):
        raise TypeError(f"latent transition field {key!r} must contain only strings")
    return result


def _required_number(data: dict[str, object], key: str) -> float:
    value = data.get(key)
    if not isinstance(value, int | float):
        raise TypeError(f"latent field {key!r} must be a number")
    return float(value)


def _loads_object(payload: str) -> dict[str, object]:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise TypeError("latent payload must be an object")
    return data


def _action_id(prediction: dict[str, object]) -> str:
    action = prediction.get("action")
    if not isinstance(action, dict):
        raise TypeError("latent prediction action must be an object")
    action_id = action.get("action_id")
    if not isinstance(action_id, str):
        raise TypeError("latent prediction action_id must be a string")
    return action_id


def _mean(values: list[float] | tuple[float, ...]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 6)


def _confidence_label(sample_count: int) -> str:
    if sample_count == 0:
        return "cold"
    if sample_count < 3:
        return "warming"
    return "grounded"


def _jsonable(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
