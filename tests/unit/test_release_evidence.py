from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import sys
import tomllib
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from tools import release_evidence

ROOT = Path(__file__).parents[2]
SBOM_PATH = ROOT / "release/runtime-v1/blackcell-runtime-v1.cdx.json"
MANIFEST_PATH = ROOT / "release/runtime-v1/verification-manifest.json"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _runtime_closure() -> tuple[set[str], set[str]]:
    with (ROOT / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)
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


def test_release_evidence_verifier_reproduces_canonical_bytes() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "tools/release_evidence.py",
            "verify",
            "--repo-root",
            ".",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    manifest = _json(MANIFEST_PATH)
    assert result == {
        "candidate_id": manifest["release"]["candidate_id"],
        "component_count": manifest["scope"]["runtime_component_count_including_blackcell"],
        "material_count": manifest["source"]["material_count"],
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


@pytest.mark.parametrize(
    "drift_path",
    (
        "README.md",
        "release/runtime-v1/blackcell-runtime-v1.cdx.json",
    ),
)
def test_release_evidence_verifier_fails_closed_on_drift(
    tmp_path: Path,
    drift_path: str,
) -> None:
    manifest = _json(MANIFEST_PATH)
    material_paths = [item["path"] for item in manifest["source"]["materials"]]
    for relative in (
        *material_paths,
        "release/runtime-v1/blackcell-runtime-v1.cdx.json",
        "release/runtime-v1/verification-manifest.json",
    ):
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    drift = tmp_path / drift_path
    drift.write_bytes(drift.read_bytes() + b"\n")
    completed = subprocess.run(
        [
            sys.executable,
            str(tmp_path / "tools/release_evidence.py"),
            "verify",
            "--repo-root",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 1
    failure = json.loads(completed.stderr)
    assert failure["schema_version"] == "runtime-v1-release-evidence-result/v1"
    assert failure["status"] == "fail"
    assert "drift" in failure["error"]


def test_cyclonedx_sbom_is_the_locked_runtime_dependency_closure() -> None:
    sbom = _json(SBOM_PATH)
    selected, direct = _runtime_closure()
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


def test_verification_manifest_binds_every_declared_material_and_output() -> None:
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

    for item in materials:
        path = ROOT / item["path"]
        payload = path.read_bytes()
        assert path.is_file() and not path.is_symlink()
        assert item["mode"] == f"{stat.S_IMODE(path.stat().st_mode):04o}"
        assert item["size"] == len(payload)
        assert item["sha256"] == _sha256(payload)

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
