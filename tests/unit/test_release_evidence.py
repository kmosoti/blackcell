from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import tomllib
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from tools import release_evidence

ROOT = Path(__file__).parents[2]
SBOM_PATH = ROOT / "release/runtime-v1/blackcell-runtime-v1.cdx.json"
MANIFEST_PATH = ROOT / "release/runtime-v1/verification-manifest.json"
AC00_BASELINE_SHA = "eb05e034f3e6bdcef83167df6dcfdc8a1eaf06f0"
HISTORICAL_EVIDENCE_ROOTS = (
    Path("docs/decisions/runtime-v1"),
    Path("release/runtime-v1"),
)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _runtime_closure(lock_bytes: bytes) -> tuple[set[str], set[str]]:
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    root = packages["blackcell"]
    selected = {"blackcell"}
    pending = deque(item["name"] for item in root["dependencies"])
    while pending:
        name = pending.popleft()
        if name in selected:
            continue
        selected.add(name)
        pending.extend(item["name"] for item in packages[name].get("dependencies", []))
    direct = {item["name"] for item in root["dependencies"]}
    return selected, direct


def _synthetic_release_repository(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    for relative in (
        release_evidence.CONFIG_PATH,
        Path("pyproject.toml"),
        Path("uv.lock"),
    ):
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    (repo / "README.md").write_text("synthetic runtime-v1 source\n", encoding="utf-8")
    monkeypatch.setattr(
        release_evidence,
        "MATERIAL_FILES",
        (
            Path("README.md"),
            Path("pyproject.toml"),
            Path("uv.lock"),
            release_evidence.CONFIG_PATH,
        ),
    )
    monkeypatch.setattr(release_evidence, "MATERIAL_DIRECTORIES", ())
    monkeypatch.setattr(
        release_evidence,
        "EXCLUDED_MATERIALS",
        frozenset({release_evidence.SBOM_PATH, release_evidence.MANIFEST_PATH}),
    )
    monkeypatch.setattr(release_evidence, "EVIDENCE_PATHS", ())
    return release_evidence._generate(repo)


def _git_blob(revision: str, relative: Path) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"{revision}:{relative.as_posix()}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    return completed.stdout


def test_release_evidence_tool_reproduces_synthetic_canonical_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = _synthetic_release_repository(tmp_path, monkeypatch)
    verified = release_evidence._verify(tmp_path)

    assert verified == {
        "candidate_id": generated["candidate_id"],
        "component_count": generated["component_count"],
        "material_count": 4,
        "schema_version": "runtime-v1-release-evidence-result/v1",
        "status": "pass",
    }


def test_release_materials_exclude_git_ignored_local_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github = tmp_path / ".github"
    workflows = github / "workflows"
    workflows.mkdir(parents=True)
    (tmp_path / ".gitignore").write_text(".github/*.env\n", encoding="utf-8")
    (workflows / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    (github / "new.yml").write_text("name: candidate\n", encoding="utf-8")
    (github / "blackcell.env").write_text("LOCAL_ONLY=true\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", ".gitignore", ".github/workflows/ci.yml"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setattr(release_evidence, "MATERIAL_FILES", ())
    monkeypatch.setattr(release_evidence, "MATERIAL_DIRECTORIES", (Path(".github"),))
    monkeypatch.setattr(release_evidence, "EXCLUDED_MATERIALS", frozenset())

    assert release_evidence._material_paths(tmp_path) == (
        Path(".github/new.yml"),
        Path(".github/workflows/ci.yml"),
    )


def test_release_material_modes_are_independent_of_checkout_umask(tmp_path: Path) -> None:
    regular = tmp_path / "regular.txt"
    executable = tmp_path / "executable.sh"
    regular.write_text("regular\n", encoding="utf-8")
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    regular.chmod(0o664)
    executable.chmod(0o775)

    assert release_evidence._file_record(tmp_path, Path("regular.txt"))["mode"] == "0644"
    assert release_evidence._file_record(tmp_path, Path("executable.sh"))["mode"] == "0755"


@pytest.mark.parametrize(
    "drift_path",
    (
        "README.md",
        "release/runtime-v1/blackcell-runtime-v1.cdx.json",
    ),
)
def test_release_evidence_verifier_fails_closed_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_path: str,
) -> None:
    _synthetic_release_repository(tmp_path, monkeypatch)

    drift = tmp_path / drift_path
    drift.write_bytes(drift.read_bytes() + b"\n")
    with pytest.raises(release_evidence.ReleaseEvidenceError, match="drift"):
        release_evidence._verify(tmp_path)


def test_historical_cyclonedx_sbom_is_the_ratified_locked_runtime_dependency_closure() -> None:
    sbom = _json(SBOM_PATH)
    selected, direct = _runtime_closure(_git_blob(AC00_BASELINE_SHA, Path("uv.lock")))
    components = sbom["components"]
    assert isinstance(components, list)
    dependencies = sbom["dependencies"]
    assert isinstance(dependencies, list)
    root = sbom["metadata"]["component"]

    assert sbom["$schema"] == "https://cyclonedx.org/schema/bom-1.7.schema.json"
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.7"
    assert root["name"] == "blackcell"
    assert root["version"] == "0.2.0"
    assert sbom["metadata"]["lifecycles"] == [{"phase": "pre-build"}]
    assert [item["name"] for item in components] == sorted(selected - {"blackcell"})

    refs = {root["bom-ref"], *(item["bom-ref"] for item in components)}
    assert len(refs) == len(selected)
    assert {item["ref"] for item in dependencies} == refs
    assert all(set(item["dependsOn"]) <= refs for item in dependencies)
    root_dependencies = next(item for item in dependencies if item["ref"] == root["bom-ref"])
    assert {
        reference.removeprefix("pkg:pypi/").split("@", maxsplit=1)[0]
        for reference in root_dependencies["dependsOn"]
    } == direct
    assert {"pytest", "pytest-cov", "ruff", "ty", "hypothesis", "mutmut"}.isdisjoint(selected)


def test_historical_runtime_v1_evidence_matches_the_ratified_ac00_inventory() -> None:
    completed = subprocess.run(
        [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            AC00_BASELINE_SHA,
            "--",
            *(path.as_posix() for path in HISTORICAL_EVIDENCE_ROOTS),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    baseline_paths = tuple(Path(line) for line in completed.stdout.splitlines())
    current_paths = tuple(
        sorted(
            relative
            for root in HISTORICAL_EVIDENCE_ROOTS
            for path in (ROOT / root).rglob("*")
            if path.is_file() and not path.is_symlink()
            for relative in (path.relative_to(ROOT),)
        )
    )

    assert current_paths == baseline_paths
    for relative in baseline_paths:
        assert (ROOT / relative).read_bytes() == _git_blob(AC00_BASELINE_SHA, relative)


def test_frozen_verification_manifest_is_canonical_and_binds_its_sbom() -> None:
    manifest = _json(MANIFEST_PATH)
    source = manifest["source"]
    materials = source["materials"]
    assert isinstance(materials, list)
    paths = [item["path"] for item in materials]

    assert manifest["schema_version"] == "runtime-v1-verification-manifest/v1"
    assert source["material_count"] == len(materials)
    assert paths == sorted(set(paths))
    assert "release/runtime-v1/verification-manifest.json" not in paths
    assert "release/runtime-v1/blackcell-runtime-v1.cdx.json" not in paths
    assert "docs/decisions/runtime-v1/wp27-release-evidence.json" not in paths
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in paths)
    assert all(item["mode"] in {"0644", "0755"} for item in materials)
    assert all(isinstance(item["size"], int) and item["size"] >= 0 for item in materials)
    assert all(
        isinstance(item["sha256"], str)
        and len(item["sha256"]) == len("sha256:") + 64
        and item["sha256"].startswith("sha256:")
        for item in materials
    )

    canonical_materials = (
        json.dumps(materials, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()
    assert source["materials_digest"] == _sha256(canonical_materials)
    assert manifest["release"]["candidate_id"] == source["materials_digest"]

    artifact = manifest["artifacts"][0]
    sbom_bytes = SBOM_PATH.read_bytes()
    assert artifact["path"] == "release/runtime-v1/blackcell-runtime-v1.cdx.json"
    assert artifact["sha256"] == _sha256(sbom_bytes)
    assert artifact["size"] == len(sbom_bytes)


def test_release_manifest_commands_and_boundaries_are_explicit() -> None:
    manifest = _json(MANIFEST_PATH)
    commands = {item["id"]: item for item in manifest["verification"]}

    assert set(commands) == {
        "locked-environment",
        "release-evidence",
        "recorded-operator-example",
        "ruff-format",
        "ruff-check",
        "types",
        "full-suite",
        "rootless-podman",
    }
    assert commands["full-suite"]["argv"] == [
        "uv",
        "run",
        "pytest",
        "--cov=blackcell",
        "--cov-report=term-missing",
    ]
    assert commands["rootless-podman"]["environment"] == {"BLACKCELL_RUN_PODMAN_TESTS": "1"}
    assert manifest["release"]["status"] == "evidence-complete-unpublished"
    assert manifest["release"]["publication_performed"] is False
    assert set(manifest["boundaries"].values()) == {False}
    assert manifest["scope"]["sbom"] == "pre-build locked Python runtime dependency closure"


def test_recorded_example_is_syntax_checked_isolated_and_credential_free() -> None:
    example = ROOT / "examples/runtime-v1/recorded-operator.sh"
    completed = subprocess.run(
        ["bash", "-n", str(example)],
        check=False,
        capture_output=True,
        text=True,
    )
    text = example.read_text(encoding="utf-8")

    assert completed.returncode == 0, completed.stderr
    assert stat.S_IMODE(example.stat().st_mode) == 0o755
    assert "mktemp -d" in text
    assert "--model recorded" in text
    assert "operator replay" in text
    assert "rm -rf" not in text
    assert "--model codex" not in text
    assert "BLACKCELL_API_TOKEN" not in text


def test_release_guide_states_the_scoped_sbom_and_non_publication_boundary() -> None:
    guide = (ROOT / "docs/guides/runtime-v1-release.md").read_text(encoding="utf-8")
    release_readme = (ROOT / "release/runtime-v1/README.md").read_text(encoding="utf-8")

    assert "CycloneDX 1.7 pre-build SBOM" in guide
    assert "transitive closure of its non-development Python dependencies" in guide
    assert "does not build or publish" in guide
    assert "verify --repo-root ." in guide
    assert "unpublished runtime-v1 evidence bundle" in release_readme
