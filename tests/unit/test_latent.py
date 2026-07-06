import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from blackcell.cli.app import app
from blackcell.latent import (
    LatentTransition,
    encode_world_state,
    load_transitions,
    predict_next_states,
    record_simulation,
    simulate_transition,
    summarize_ledger,
    summarize_prediction_stats,
)
from blackcell.world import observe_repo
from tests.cli_runner import CycloptsCliRunner

runner = CycloptsCliRunner()


def test_latent_encoder_is_deterministic_and_inspectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    snapshot = observe_repo()

    first = encode_world_state(snapshot)
    second = encode_world_state(snapshot)

    assert first == second
    assert first.encoder_version == "latent-v0-deterministic"
    assert first.policy["training_enabled"] is False
    assert first.structural["has_docs"] is True
    assert len(first.semantic) == 8


def test_latent_predictor_scores_three_actions_without_training(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    state = encode_world_state(observe_repo())

    prediction_set = predict_next_states(state)

    assert len(prediction_set.predictions) == 3
    assert {prediction.sample_count for prediction in prediction_set.predictions} == {0}
    assert all(
        prediction.predictor_version == "transition-memory-v0-non-parametric"
        for prediction in prediction_set.predictions
    )
    assert all(
        "low_sample_count" in prediction.likely_surprises
        for prediction in prediction_set.predictions
    )
    assert {prediction.confidence_label for prediction in prediction_set.predictions} == {"cold"}
    assert all(
        "cold_action_memory" in prediction.likely_surprises
        for prediction in prediction_set.predictions
    )


def test_latent_predictor_counts_only_matching_action_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    state = encode_world_state(observe_repo())
    initial_predictions = predict_next_states(state).predictions
    first_action = initial_predictions[0].action
    second_action = initial_predictions[1].action
    third_action = initial_predictions[2].action
    memory = (
        LatentTransition(
            transition_id="latent-transition:matching-first",
            from_state_id=state.state_id,
            action_id=first_action.action_id,
            predicted_state_id="latent-state:predicted",
            actual_state_id="latent-state:actual",
            error_id="latent-error:matching-first",
            outcome="simulated",
        ),
        LatentTransition(
            transition_id="latent-transition:matching-second",
            from_state_id=state.state_id,
            action_id=second_action.action_id,
            predicted_state_id="latent-state:predicted",
            actual_state_id="latent-state:actual",
            error_id="latent-error:matching-second",
            outcome="simulated",
        ),
        LatentTransition(
            transition_id="latent-transition:wrong-state",
            from_state_id="latent-state:other",
            action_id=first_action.action_id,
            predicted_state_id="latent-state:predicted",
            actual_state_id="latent-state:actual",
            error_id="latent-error:wrong-state",
            outcome="simulated",
        ),
    )

    prediction_set = predict_next_states(state, transition_memory=memory)

    samples_by_action = {
        prediction.action.action_id: prediction.sample_count
        for prediction in prediction_set.predictions
    }
    assert samples_by_action[first_action.action_id] == 1
    assert samples_by_action[second_action.action_id] == 1
    assert samples_by_action[third_action.action_id] == 0


def test_latent_simulation_emits_error_transition_and_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = simulate_transition(observe_repo())

    assert result.state.state_id.startswith("latent-state:")
    assert result.prediction.prediction_id.startswith("latent-prediction:")
    assert result.error.error_id.startswith("latent-error:")
    assert result.transition.transition_id.startswith("latent-transition:")
    assert result.self_supervision_sample.task == "next_state_prediction"
    assert result.self_supervision_sample.accepted_for_training is False


def test_latent_ledger_records_simulation_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "latent.sqlite3"
    simulation = simulate_transition(observe_repo())

    first = record_simulation(simulation, path=db)
    second = record_simulation(simulation, path=db)
    summary = summarize_ledger(path=db)

    assert first == second
    assert summary.path == db
    assert summary.schema_version == 1
    assert summary.state_count == 3
    assert summary.prediction_count == 1
    assert summary.error_count == 1
    assert summary.transition_count == 1
    assert summary.sample_count == 1
    assert load_transitions(path=db) == (simulation.transition,)


def test_latent_predictor_uses_loaded_ledger_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "latent.sqlite3"
    record_simulation(simulate_transition(observe_repo()), path=db)

    state = encode_world_state(observe_repo())
    prediction_set = predict_next_states(state, transition_memory=load_transitions(path=db))

    samples_by_action = {
        prediction.action.action_id: prediction.sample_count
        for prediction in prediction_set.predictions
    }
    assert samples_by_action["action:observe-validate"] == 1
    assert samples_by_action["action:harness-dry-run"] == 0
    assert samples_by_action["action:docs-spec-sync"] == 0
    labels_by_action = {
        prediction.action.action_id: prediction.confidence_label
        for prediction in prediction_set.predictions
    }
    assert labels_by_action["action:observe-validate"] == "warming"
    assert labels_by_action["action:harness-dry-run"] == "cold"


def test_latent_prediction_stats_summarize_action_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "latent.sqlite3"
    simulation = simulate_transition(observe_repo())

    record_simulation(simulation, path=db)
    stats = summarize_prediction_stats(path=db)

    assert stats.path == db
    assert len(stats.action_stats) == 1
    action = stats.action_stats[0]
    assert action.action_id == simulation.prediction.action.action_id
    assert action.sample_count == 1
    assert action.mean_semantic_distance == simulation.error.semantic_distance
    assert action.surprise_count == 1
    assert action.confidence_label == "warming"


def test_ledger_stats_can_ground_prediction_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "latent.sqlite3"
    simulation = simulate_transition(observe_repo())
    record_simulation(simulation, path=db)
    state = encode_world_state(observe_repo())
    action_id = simulation.prediction.action.action_id

    _insert_prediction_error_pair(db, action_id=action_id, suffix="two")
    _insert_prediction_error_pair(db, action_id=action_id, suffix="three")
    _insert_transition(db, state_id=state.state_id, action_id=action_id, suffix="two")
    _insert_transition(db, state_id=state.state_id, action_id=action_id, suffix="three")

    stats = summarize_prediction_stats(path=db)
    predictions = predict_next_states(
        state,
        transition_memory=load_transitions(path=db),
        confidence_labels_by_action={
            action.action_id: action.confidence_label for action in stats.action_stats
        },
    )

    observe_prediction = predictions.predictions[0]
    assert observe_prediction.sample_count == 3
    assert observe_prediction.confidence_label == "grounded"
    assert observe_prediction.likely_surprises == ()


def test_action_level_stats_do_not_raise_confidence_without_matching_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "latent.sqlite3"
    state = encode_world_state(observe_repo())
    action_id = "action:observe-validate"
    for index in range(3):
        suffix = f"other-state-{index}"
        _insert_prediction_error_pair(db, action_id=action_id, suffix=suffix)
        _insert_transition(
            db,
            state_id=f"latent-state:other-{index}",
            action_id=action_id,
            suffix=suffix,
        )

    stats = summarize_prediction_stats(path=db)
    predictions = predict_next_states(
        state,
        transition_memory=load_transitions(path=db),
        confidence_labels_by_action={
            action.action_id: action.confidence_label for action in stats.action_stats
        },
    )

    observe_prediction = predictions.predictions[0]
    assert stats.action_stats[0].confidence_label == "grounded"
    assert observe_prediction.sample_count == 0
    assert observe_prediction.confidence_label == "cold"
    assert "cold_action_memory" in observe_prediction.likely_surprises


def test_action_level_stats_cannot_exceed_matching_state_sample_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "latent.sqlite3"
    state = encode_world_state(observe_repo())
    action_id = "action:observe-validate"
    _insert_transition(db, state_id=state.state_id, action_id=action_id, suffix="matching")
    for index in range(3):
        suffix = f"stats-only-{index}"
        _insert_prediction_error_pair(db, action_id=action_id, suffix=suffix)
        _insert_transition(
            db,
            state_id=f"latent-state:other-{index}",
            action_id=action_id,
            suffix=suffix,
        )

    stats = summarize_prediction_stats(path=db)
    predictions = predict_next_states(
        state,
        transition_memory=load_transitions(path=db),
        confidence_labels_by_action={
            action.action_id: action.confidence_label for action in stats.action_stats
        },
    )

    observe_prediction = predictions.predictions[0]
    assert stats.action_stats[0].confidence_label == "grounded"
    assert observe_prediction.sample_count == 1
    assert observe_prediction.confidence_label == "warming"


def test_latent_ledger_load_is_read_only_for_existing_non_ledger_db(tmp_path: Path) -> None:
    db = tmp_path / "other.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("create table marker (value text not null)")
        connection.execute("insert into marker(value) values ('kept')")

    assert load_transitions(path=db) == ()

    with sqlite3.connect(db) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type = 'table'")
        }
        marker = connection.execute("select value from marker").fetchone()[0]
    assert tables == {"marker"}
    assert marker == "kept"


def test_default_ledger_record_then_predict_reuses_memory_in_real_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo_files(tmp_path)
    (tmp_path / ".gitignore").write_text(".blackcell/\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)

    record_simulation(simulate_transition(observe_repo()))
    state = encode_world_state(observe_repo())
    prediction_set = predict_next_states(state, transition_memory=load_transitions())

    samples_by_action = {
        prediction.action.action_id: prediction.sample_count
        for prediction in prediction_set.predictions
    }
    assert samples_by_action["action:observe-validate"] == 1
    assert samples_by_action["action:harness-dry-run"] == 0


def test_latent_cli_commands_are_json_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    encode_result = runner.invoke(app, ["latent", "encode"], catch_exceptions=False)
    predict_result = runner.invoke(app, ["latent", "predict"], catch_exceptions=False)
    errors_result = runner.invoke(app, ["latent", "errors"], catch_exceptions=False)
    db = tmp_path / "latent.sqlite3"
    record_result = runner.invoke(
        app,
        ["latent", "record", "--db", str(db)],
        catch_exceptions=False,
    )
    ledger_result = runner.invoke(
        app,
        ["latent", "ledger", "--db", str(db)],
        catch_exceptions=False,
    )
    memory_predict_result = runner.invoke(
        app,
        ["latent", "predict", "--db", str(db)],
        catch_exceptions=False,
    )
    stats_result = runner.invoke(
        app,
        ["latent", "stats", "--db", str(db)],
        catch_exceptions=False,
    )

    assert encode_result.exit_code == 0
    assert json.loads(encode_result.stdout)["state_id"].startswith("latent-state:")
    assert predict_result.exit_code == 0
    assert len(json.loads(predict_result.stdout)["predictions"]) == 3
    assert errors_result.exit_code == 0
    assert (
        json.loads(errors_result.stdout)["self_supervision_sample"]["task"]
        == "next_state_prediction"
    )
    assert record_result.exit_code == 0
    assert json.loads(record_result.stdout)["transition_id"].startswith("latent-transition:")
    assert ledger_result.exit_code == 0
    ledger_payload = json.loads(ledger_result.stdout)
    assert ledger_payload["transition_count"] == 1
    assert ledger_payload["sample_count"] == 1
    assert memory_predict_result.exit_code == 0
    memory_predictions = json.loads(memory_predict_result.stdout)["predictions"]
    assert memory_predictions[0]["sample_count"] == 1
    assert memory_predictions[0]["confidence_label"] == "warming"
    assert memory_predictions[1]["sample_count"] == 0
    assert memory_predictions[1]["confidence_label"] == "cold"
    assert stats_result.exit_code == 0
    stats_payload = json.loads(stats_result.stdout)
    assert stats_payload["action_stats"][0]["confidence_label"] == "warming"
    assert stats_payload["action_stats"][0]["sample_count"] == 1


def _write_repo(path: Path) -> None:
    (path / ".git").mkdir()
    _write_repo_files(path)


def _write_repo_files(path: Path) -> None:
    (path / "README.md").write_text("# Test\n", encoding="utf-8")
    (path / "docs").mkdir()
    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")


def _insert_prediction_error_pair(db: Path, *, action_id: str, suffix: str) -> None:
    with sqlite3.connect(db) as connection:
        _ensure_test_ledger_tables(connection)
        prediction_id = f"latent-prediction:{suffix}"
        connection.execute(
            "insert into latent_predictions(prediction_id, payload_json) values (?, ?)",
            (
                prediction_id,
                json.dumps(
                    {
                        "prediction_id": prediction_id,
                        "action": {"action_id": action_id},
                    }
                ),
            ),
        )
        connection.execute(
            "insert into latent_errors(error_id, payload_json) values (?, ?)",
            (
                f"latent-error:{suffix}",
                json.dumps(
                    {
                        "prediction_id": prediction_id,
                        "semantic_distance": 0.0,
                        "surprise": "none",
                    }
                ),
            ),
        )


def _insert_transition(db: Path, *, state_id: str, action_id: str, suffix: str) -> None:
    transition_id = f"latent-transition:{suffix}"
    with sqlite3.connect(db) as connection:
        _ensure_test_ledger_tables(connection)
        connection.execute(
            "insert into latent_transitions(transition_id, payload_json) values (?, ?)",
            (
                transition_id,
                json.dumps(
                    {
                        "transition_id": transition_id,
                        "from_state_id": state_id,
                        "action_id": action_id,
                        "predicted_state_id": f"latent-state:predicted-{suffix}",
                        "actual_state_id": f"latent-state:actual-{suffix}",
                        "error_id": f"latent-error:{suffix}",
                        "outcome": "simulated",
                    }
                ),
            ),
        )


def _ensure_test_ledger_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
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
        """
    )
