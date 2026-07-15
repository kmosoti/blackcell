from pathlib import Path
from typing import Any

import yaml


def test_containerfile_preserves_the_reproducible_development_target() -> None:
    text = Path("Containerfile").read_text(encoding="utf-8")
    development = text.partition("FROM uv-base AS development")[2].partition(
        "FROM uv-base AS runtime-builder"
    )[0]

    assert "ghcr.io/astral-sh/uv:python3.14-trixie-slim" in text
    assert "uv sync --locked --all-groups" in development
    assert "build-essential" in development
    assert "ripgrep" in development
    assert "NVM_VERSION" not in text
    assert "curl" not in text
    assert "npm" not in text
    assert "opencode-ai" not in text


def test_runtime_image_is_locked_minimal_non_root_and_process_shaped() -> None:
    text = Path("Containerfile").read_text(encoding="utf-8")
    project = Path("pyproject.toml").read_text(encoding="utf-8")
    builder = text.partition("FROM uv-base AS runtime-builder")[2].partition(
        "FROM python:3.14-slim-trixie AS runtime"
    )[0]
    runtime = text.partition("FROM python:3.14-slim-trixie AS runtime")[2]

    assert "uv sync --locked --no-dev --no-editable" in builder
    assert '"granian[pname]>=2.7.9,<3"' in project
    assert "COPY src ./src" in builder
    assert "COPY . ." not in builder
    assert "COPY --from=runtime-builder" in runtime
    assert "USER 10001:10001" in runtime
    assert 'ENTRYPOINT ["blackcell-runtime"]' in runtime
    assert 'CMD ["api"]' in runtime
    assert "STOPSIGNAL SIGTERM" in runtime
    assert "HEALTHCHECK" in runtime
    assert "PYTHONDONTWRITEBYTECODE=1" in runtime
    assert "GIT_OPTIONAL_LOCKS=0" in runtime
    assert "build-essential" not in runtime
    assert "ripgrep" not in runtime
    assert "uv sync" not in runtime
    assert "COPY . ." not in runtime


def test_compose_runs_api_and_worker_from_one_restricted_image() -> None:
    compose = _compose()
    api = compose["services"]["blackcell-api"]
    worker = compose["services"]["blackcell-worker"]

    assert api["image"] == worker["image"]
    assert api["build"] == worker["build"]
    assert api["build"]["target"] == "runtime"
    assert api["command"] == ["api"]
    assert worker["command"] == ["worker"]
    assert worker["depends_on"] == {"blackcell-api": {"condition": "service_healthy"}}
    for service in (api, worker):
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["tmpfs"] == ["/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777"]
        assert service["restart"] == "unless-stopped"
        assert service["stop_grace_period"] == "35s"


def test_compose_preserves_state_repository_network_and_secret_boundaries() -> None:
    compose = _compose()
    api = compose["services"]["blackcell-api"]
    worker = compose["services"]["blackcell-worker"]
    state_mount, repository_mount = api["volumes"]

    assert state_mount == {
        "type": "volume",
        "source": "blackcell-data",
        "target": "/var/lib/blackcell",
    }
    assert repository_mount == {
        "type": "bind",
        "source": "${BLACKCELL_REPOSITORY_PATH:-.}",
        "target": "/workspace/repository",
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    assert worker["volumes"] == api["volumes"]
    assert "blackcell-data" in compose["volumes"]
    assert api["environment"]["BLACKCELL_DATA_DIR"] == "/var/lib/blackcell/data"
    assert api["environment"]["BLACKCELL_REPOSITORY_ROOT"] == "/workspace/repository"
    assert api["environment"]["BLACKCELL_API_TOKEN"].startswith("${BLACKCELL_API_TOKEN:?")
    assert api["environment"] == worker["environment"]
    assert api["ports"] == [
        {
            "target": 8080,
            "published": "${BLACKCELL_PUBLISHED_PORT:-8080}",
            "host_ip": "127.0.0.1",
            "protocol": "tcp",
        }
    ]
    assert "healthcheck" not in api
    assert worker["healthcheck"]["test"] == [
        "CMD-SHELL",
        "python -c 'import os; os.kill(1, 0)'",
    ]


def test_container_contract_does_not_mount_or_embed_credentials_or_engine_authority() -> None:
    containerfile = Path("Containerfile").read_text(encoding="utf-8")
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    devcontainer = Path(".devcontainer/devcontainer.json").read_text(encoding="utf-8")
    combined = "\n".join((containerfile, compose, devcontainer)).casefold()

    assert '"target": "development"' in devcontainer
    assert "blackcell-opencode" not in combined
    assert "/root/.config/opencode" not in combined
    assert "openai_api_key" not in combined
    assert "codex_home" not in combined
    assert "/run/podman/podman.sock" not in combined
    assert "/var/run/docker.sock" not in combined
    assert "privileged:" not in combined
    assert "network_mode: host" not in combined
    assert "pid: host" not in combined
    assert "api_token=" not in containerfile.casefold()


def test_node_version_file_is_not_referenced_by_core_runtime() -> None:
    assert ".nvmrc" not in Path("Containerfile").read_text(encoding="utf-8")


def _compose() -> dict[str, Any]:
    loaded = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded
