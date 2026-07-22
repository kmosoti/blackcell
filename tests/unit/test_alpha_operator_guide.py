from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from blackcell.cli.app import app
from blackcell.config.alpha_verify import (
    ALPHA_VERIFY_CONFIG_FILE_ENV,
    AlphaVerifyWorkerRuntimeConfig,
    load_alpha_verify_config,
)
from blackcell.interfaces.http import (
    AlphaCancelRunRequest,
    AlphaIntentRequest,
    AlphaPlanRequest,
    AlphaProjectRequest,
    AlphaRunRequest,
    decode_contract,
)
from tests.cli_runner import CycloptsCliRunner

ROOT = Path(__file__).parents[2]
REQUEST_ROOT = ROOT / "examples" / "alpha" / "requests"
QUICKSTART_PATH = ROOT / "docs" / "guides" / "alpha-operator-quickstart.md"
VERIFY_GUIDE_PATH = ROOT / "docs" / "guides" / "alpha-verify-configuration.md"
VERIFY_EXAMPLE_PATH = ROOT / "examples" / "alpha" / "alpha-verify.json"


def test_alpha_request_templates_decode_and_cross_bind() -> None:
    project = decode_contract(
        (REQUEST_ROOT / "project.template.json").read_bytes(), AlphaProjectRequest
    )
    intent = decode_contract(
        (REQUEST_ROOT / "intent.template.json").read_bytes(), AlphaIntentRequest
    )
    plan = decode_contract((REQUEST_ROOT / "plan.template.json").read_bytes(), AlphaPlanRequest)
    run = decode_contract((REQUEST_ROOT / "run.template.json").read_bytes(), AlphaRunRequest)
    cancel = decode_contract(
        (REQUEST_ROOT / "cancel.template.json").read_bytes(), AlphaCancelRunRequest
    )

    assert project.root == "/ABSOLUTE/PATH/TO/PROJECT"
    assert project.configuration_digest == "sha256:" + "0" * 64
    assert plan.base_commit == "0" * 40
    assert intent.project_id == plan.project_id == run.project_id == project.project_id
    assert plan.intent_id == run.intent_id == intent.intent_id
    assert run.plan_id == plan.plan_id
    assert run.run_id == "alpha-run"
    assert cancel.idempotency_key == "alpha-run-cancel-v1"

    assert len(plan.nodes) == 1
    node = plan.nodes[0]
    assert node.effects == ("repository-read", "repository-write", "process")
    assert node.allowed_paths == ("src/example.py",)
    assert node.budget.max_changed_files == 1
    assert node.checks[0].argv[0] == "python"
    assert (
        len(
            {
                project.idempotency_key,
                intent.idempotency_key,
                plan.idempotency_key,
                run.idempotency_key,
                cancel.idempotency_key,
            }
        )
        == 5
    )


def test_alpha_guides_bind_live_commands_config_and_nonclaims(tmp_path: Path) -> None:
    quickstart = QUICKSTART_PATH.read_text(encoding="utf-8")
    verify_guide = VERIFY_GUIDE_PATH.read_text(encoding="utf-8")
    normalized_quickstart = " ".join(quickstart.split())

    required_commands = {
        "uv run blackcell project check": ("project", "check"),
        "uv run blackcell daemon foreground": ("daemon", "foreground"),
        "uv run blackcell daemon status": ("daemon", "status"),
        "uv run blackcell alpha project register": ("alpha", "project", "register"),
        "uv run blackcell alpha intent accept": ("alpha", "intent", "accept"),
        "uv run blackcell alpha plan accept": ("alpha", "plan", "accept"),
        "uv run blackcell alpha run submit": ("alpha", "run", "submit"),
        "uv run blackcell alpha run status": ("alpha", "run", "status"),
        "uv run blackcell alpha run replay": ("alpha", "run", "replay"),
        "uv run blackcell alpha run cancel": ("alpha", "run", "cancel"),
        "uv run blackcell alpha events list": ("alpha", "events", "list"),
        "uv run blackcell alpha tui": ("alpha", "tui"),
    }
    runner = CycloptsCliRunner()
    for command, tokens in required_commands.items():
        assert command in quickstart
        help_result = runner.invoke(app, [*tokens, "--help"], catch_exceptions=False)
        assert help_result.exit_code == 0, command

    assert "uv run blackcell-runtime alpha-verify-worker --once" in verify_guide
    assert "http://127.0.0.1:8080/alpha" in quickstart
    assert "Never submit new alpha work through `/api/v1/runs`" in normalized_quickstart
    assert "not an installation or alpha-release claim" in normalized_quickstart
    assert "public PyRatatui client is locked and measured" in normalized_quickstart
    assert "not a runnable project or live-provider proof" in normalized_quickstart
    assert "No package, tag, release, or deployment" in normalized_quickstart

    for guide_path, guide in (
        (QUICKSTART_PATH, quickstart),
        (VERIFY_GUIDE_PATH, verify_guide),
    ):
        for target in _local_links(guide):
            linked = (guide_path.parent / target).resolve()
            assert linked.is_relative_to(ROOT.resolve())
            assert linked.exists(), target

    block = re.search(r"```json\n(?P<document>.*?)\n```", verify_guide, re.DOTALL)
    assert block is not None
    documented_config = json.loads(block.group("document"))
    example_config = json.loads(VERIFY_EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert documented_config == example_config

    repository = tmp_path / "repository"
    repository.mkdir()
    source = tmp_path / "alpha-verify.json"
    source.write_bytes(VERIFY_EXAMPLE_PATH.read_bytes())
    source.chmod(0o600)
    config = load_alpha_verify_config(
        {ALPHA_VERIFY_CONFIG_FILE_ENV: str(source)},
        repository_root=repository,
    )
    assert isinstance(config, AlphaVerifyWorkerRuntimeConfig)
    assert config.worker.worker_id == "alpha-verifier.local-1"
    assert config.worker.supervisor_id == "alpha-verify-supervisor.local-1"
    assert config.worker.worker_id != config.worker.supervisor_id


def _local_links(text: str) -> tuple[str, ...]:
    targets = {
        target.split("#", maxsplit=1)[0]
        for target in re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", text)
        if target and not target.startswith(("https://", "http://", "mailto:", "#"))
    }
    return tuple(sorted(cast("set[str]", targets)))
