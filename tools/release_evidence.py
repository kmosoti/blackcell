#!/usr/bin/env python3
"""Generate and verify the deterministic runtime-v1 release-evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
import uuid
from collections import deque
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

CONFIG_PATH = Path("release/runtime-v1/release.toml")
SBOM_PATH = Path("release/runtime-v1/blackcell-runtime-v1.cdx.json")
MANIFEST_PATH = Path("release/runtime-v1/verification-manifest.json")

MATERIAL_FILES = (
    Path(".python-version"),
    Path("Containerfile"),
    Path("LICENSE"),
    Path("README.md"),
    Path("blackcell.plan.yaml"),
    Path("compose.yaml"),
    Path("pyproject.toml"),
    Path("uv.lock"),
)
MATERIAL_DIRECTORIES = (
    Path(".github"),
    Path("architecture"),
    Path("docs"),
    Path("examples"),
    Path("experiments"),
    Path("release/runtime-v1"),
    Path("src"),
    Path("tests"),
    Path("tools"),
)
EXCLUDED_MATERIALS = frozenset(
    {
        SBOM_PATH,
        MANIFEST_PATH,
        Path("docs/decisions/runtime-v1/wp27-release-evidence.json"),
    }
)
EVIDENCE_PATHS = (
    Path("docs/decisions/runtime-v1/wp25-runtime-benchmark.json"),
    Path("experiments/runtime_bench/wp25-recorded.json"),
    Path("docs/decisions/runtime-v1/wp26-legacy-retirement.json"),
    Path("experiments/legacy_retirement/wp26-characterization.json"),
    Path("experiments/legacy_retirement/wp26-retirement.json"),
)
NON_MATERIAL_PARTS = frozenset({"__pycache__", ".hypothesis", ".pytest_cache", ".ruff_cache"})
VERIFICATION_COMMANDS: tuple[dict[str, object], ...] = (
    {
        "id": "locked-environment",
        "argv": ["uv", "sync", "--locked", "--all-groups"],
        "expected_exit_code": 0,
    },
    {
        "id": "release-evidence",
        "argv": [
            "uv",
            "run",
            "python",
            "tools/release_evidence.py",
            "verify",
            "--repo-root",
            ".",
        ],
        "expected_exit_code": 0,
    },
    {
        "id": "recorded-operator-example",
        "argv": ["bash", "examples/runtime-v1/recorded-operator.sh"],
        "expected_exit_code": 0,
    },
    {
        "id": "ruff-format",
        "argv": ["uv", "run", "ruff", "format", "--check", "."],
        "expected_exit_code": 0,
    },
    {
        "id": "ruff-check",
        "argv": ["uv", "run", "ruff", "check", "."],
        "expected_exit_code": 0,
    },
    {
        "id": "types",
        "argv": ["uv", "run", "ty", "check"],
        "expected_exit_code": 0,
    },
    {
        "id": "full-suite",
        "argv": [
            "uv",
            "run",
            "pytest",
            "--cov=blackcell",
            "--cov-report=term-missing",
        ],
        "expected_exit_code": 0,
    },
    {
        "id": "rootless-podman",
        "environment": {"BLACKCELL_RUN_PODMAN_TESTS": "1"},
        "argv": [
            "uv",
            "run",
            "pytest",
            "-q",
            "tests/integration/test_podman_runtime.py",
        ],
        "expected_exit_code": 0,
        "requirement": "Linux rootless Podman; recorded WP25 evidence remains authoritative.",
    },
)

JsonObject = dict[str, Any]


class ReleaseEvidenceError(RuntimeError):
    """Raised when release evidence cannot be generated or verified safely."""


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _json_bytes(value: object, *, pretty: bool = True) -> bytes:
    if pretty:
        text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True)
    else:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{text}\n".encode()


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _read_toml(path: Path) -> JsonObject:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseEvidenceError(f"cannot read TOML input: {path}") from error
    return document


def _load_config(repo_root: Path) -> JsonObject:
    config = _read_toml(repo_root / CONFIG_PATH)
    required = {
        "schema_version",
        "release_id",
        "project_version",
        "recorded_on",
        "base_sha",
        "candidate_status",
        "publication_performed",
    }
    if set(config) != required:
        raise ReleaseEvidenceError("release config keys do not match the closed contract")
    if config["schema_version"] != "runtime-v1-release-config/v1":
        raise ReleaseEvidenceError("unsupported release config schema")
    if config["release_id"] != "runtime-v1":
        raise ReleaseEvidenceError("release_id must be runtime-v1")
    if config["candidate_status"] != "evidence-complete-unpublished":
        raise ReleaseEvidenceError("candidate status must remain evidence-complete-unpublished")
    if config["publication_performed"] is not False:
        raise ReleaseEvidenceError("release evidence cannot claim publication")
    if not re.fullmatch(r"[0-9a-f]{40}", str(config["base_sha"])):
        raise ReleaseEvidenceError("base_sha must be a lowercase forty-character Git object ID")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(config["recorded_on"])):
        raise ReleaseEvidenceError("recorded_on must be an ISO calendar date")
    return config


def _require_project_identity(
    config: JsonObject,
    project: JsonObject,
    root_package: JsonObject,
) -> None:
    project_table = project.get("project")
    if not isinstance(project_table, dict):
        raise ReleaseEvidenceError("pyproject.toml has no project table")
    expected = ("blackcell", str(config["project_version"]))
    observed = (project_table.get("name"), project_table.get("version"))
    locked = (root_package.get("name"), root_package.get("version"))
    if observed != expected or locked != expected:
        raise ReleaseEvidenceError("release config, pyproject, and lockfile identity differ")


def _dependency_names(package: JsonObject) -> tuple[str, ...]:
    dependencies = package.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise ReleaseEvidenceError(f"invalid dependency list for {package.get('name')}")
    names: list[str] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict) or not isinstance(dependency.get("name"), str):
            raise ReleaseEvidenceError(f"invalid locked dependency for {package.get('name')}")
        names.append(_canonical_name(dependency["name"]))
    return tuple(names)


def _runtime_closure(lock: JsonObject) -> tuple[JsonObject, dict[str, JsonObject]]:
    raw_packages = lock.get("package")
    if not isinstance(raw_packages, list):
        raise ReleaseEvidenceError("uv.lock has no package inventory")
    packages: dict[str, JsonObject] = {}
    for raw_package in raw_packages:
        if not isinstance(raw_package, dict) or not isinstance(raw_package.get("name"), str):
            raise ReleaseEvidenceError("uv.lock contains an invalid package entry")
        package = cast("JsonObject", raw_package)
        name = _canonical_name(package["name"])
        if name in packages:
            raise ReleaseEvidenceError(f"ambiguous locked package name: {name}")
        packages[name] = package

    root = packages.get("blackcell")
    if root is None:
        raise ReleaseEvidenceError("uv.lock has no blackcell root package")

    selected: dict[str, JsonObject] = {"blackcell": root}
    pending = deque(_dependency_names(root))
    while pending:
        name = pending.popleft()
        if name in selected:
            continue
        package = packages.get(name)
        if package is None:
            raise ReleaseEvidenceError(f"locked runtime dependency is missing: {name}")
        selected[name] = package
        pending.extend(_dependency_names(package))
    return root, selected


def _direct_project_dependencies(project: JsonObject) -> set[str]:
    project_table = project.get("project")
    if not isinstance(project_table, dict):
        raise ReleaseEvidenceError("pyproject.toml has no project table")
    dependencies = project_table.get("dependencies")
    if not isinstance(dependencies, list):
        raise ReleaseEvidenceError("pyproject.toml has no runtime dependency list")
    result: set[str] = set()
    for requirement in dependencies:
        if not isinstance(requirement, str):
            raise ReleaseEvidenceError("pyproject dependency must be a string")
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        if match is None:
            raise ReleaseEvidenceError(f"cannot parse project dependency: {requirement}")
        result.add(_canonical_name(match.group(0)))
    return result


def _purl(package: JsonObject) -> str:
    name = _canonical_name(str(package["name"]))
    version = str(package["version"])
    return f"pkg:pypi/{quote(name, safe='._~-')}@{quote(version, safe='._~-')}"


def _source_property(package: JsonObject) -> str:
    source = package.get("source")
    if not isinstance(source, dict) or len(source) != 1:
        return "uv-lock"
    key, value = next(iter(source.items()))
    return f"{key}:{value}"


def _build_sbom(repo_root: Path, config: JsonObject) -> tuple[JsonObject, int]:
    project = _read_toml(repo_root / "pyproject.toml")
    lock_path = repo_root / "uv.lock"
    lock_bytes = lock_path.read_bytes()
    lock = _read_toml(lock_path)
    root_package, selected = _runtime_closure(lock)
    _require_project_identity(config, project, root_package)

    direct_project = _direct_project_dependencies(project)
    direct_locked = set(_dependency_names(root_package))
    if direct_project != direct_locked:
        raise ReleaseEvidenceError("pyproject and lockfile direct runtime dependencies differ")

    root_ref = _purl(root_package)
    components: list[JsonObject] = []
    for name in sorted(selected):
        if name == "blackcell":
            continue
        package = selected[name]
        component: JsonObject = {
            "bom-ref": _purl(package),
            "name": name,
            "properties": [
                {"name": "blackcell:uv-lock-source", "value": _source_property(package)}
            ],
            "purl": _purl(package),
            "scope": "required",
            "type": "library",
            "version": str(package["version"]),
        }
        components.append(component)

    dependencies: list[JsonObject] = []
    for name in sorted(selected):
        package = selected[name]
        child_refs = sorted(
            _purl(selected[child]) for child in set(_dependency_names(package)) if child in selected
        )
        dependencies.append({"dependsOn": child_refs, "ref": _purl(package)})

    lock_digest = _sha256_bytes(lock_bytes)
    serial = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"blackcell:{config['release_id']}:{config['project_version']}:{lock_digest}",
    )
    sbom: JsonObject = {
        "$schema": "https://cyclonedx.org/schema/bom-1.7.schema.json",
        "bomFormat": "CycloneDX",
        "components": components,
        "dependencies": dependencies,
        "metadata": {
            "component": {
                "bom-ref": root_ref,
                "licenses": [{"license": {"id": "MIT"}}],
                "name": "blackcell",
                "properties": [
                    {"name": "blackcell:sbom-scope", "value": "locked-python-runtime"},
                    {"name": "blackcell:lockfile", "value": "uv.lock"},
                    {"name": "blackcell:lockfile-sha256", "value": lock_digest},
                ],
                "purl": root_ref,
                "type": "application",
                "version": str(config["project_version"]),
            },
            "lifecycles": [{"phase": "pre-build"}],
            "timestamp": f"{config['recorded_on']}T00:00:00Z",
            "tools": {
                "components": [
                    {
                        "name": "blackcell-release-evidence",
                        "type": "application",
                        "version": "1",
                    }
                ]
            },
        },
        "serialNumber": f"urn:uuid:{serial}",
        "specVersion": "1.7",
        "version": 1,
    }
    return sbom, len(selected)


def _material_paths(repo_root: Path) -> tuple[Path, ...]:
    selected: set[Path] = set()
    for relative in MATERIAL_FILES:
        path = repo_root / relative
        if not path.is_file():
            raise ReleaseEvidenceError(f"required release material is missing: {relative}")
        selected.add(relative)
    for directory in MATERIAL_DIRECTORIES:
        root = repo_root / directory
        if not root.is_dir():
            raise ReleaseEvidenceError(
                f"required release material directory is missing: {directory}"
            )
        for path in root.rglob("*"):
            relative = path.relative_to(repo_root)
            if any(part in NON_MATERIAL_PARTS for part in relative.parts) or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ReleaseEvidenceError(
                    f"release material must not be a symbolic link: {relative}"
                )
            if path.is_file():
                selected.add(relative)
    selected -= EXCLUDED_MATERIALS
    return tuple(sorted(selected, key=lambda item: item.as_posix()))


def _file_record(repo_root: Path, relative: Path) -> JsonObject:
    path = repo_root / relative
    if path.is_symlink() or not path.is_file():
        raise ReleaseEvidenceError(f"release material is not a regular file: {relative}")
    raw = path.read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)
    return {
        "mode": f"{mode:04o}",
        "path": relative.as_posix(),
        "sha256": _sha256_bytes(raw),
        "size": len(raw),
    }


def _materials_digest(materials: list[JsonObject]) -> str:
    return _sha256_bytes(_json_bytes(materials, pretty=False))


def _build_manifest(
    repo_root: Path,
    config: JsonObject,
    sbom_bytes: bytes,
    runtime_component_count: int,
) -> JsonObject:
    materials = [_file_record(repo_root, path) for path in _material_paths(repo_root)]
    materials_digest = _materials_digest(materials)
    evidence = [_file_record(repo_root, path) for path in EVIDENCE_PATHS]
    return {
        "schema_version": "runtime-v1-verification-manifest/v1",
        "release": {
            "base_sha": config["base_sha"],
            "candidate_id": materials_digest,
            "project_version": config["project_version"],
            "publication_performed": False,
            "recorded_on": config["recorded_on"],
            "release_id": config["release_id"],
            "status": config["candidate_status"],
        },
        "source": {
            "material_count": len(materials),
            "materials": materials,
            "materials_digest": materials_digest,
        },
        "artifacts": [
            {
                "media_type": "application/vnd.cyclonedx+json",
                "path": SBOM_PATH.as_posix(),
                "schema_version": "CycloneDX/1.7",
                "sha256": _sha256_bytes(sbom_bytes),
                "size": len(sbom_bytes),
            }
        ],
        "evidence": evidence,
        "verification": list(VERIFICATION_COMMANDS),
        "scope": {
            "runtime_component_count_including_blackcell": runtime_component_count,
            "sbom": "pre-build locked Python runtime dependency closure",
            "excluded": [
                "development-only Python dependencies",
                "unbuilt container and operating-system packages",
                "host packages and services",
                "vulnerability analysis",
            ],
        },
        "boundaries": {
            "container_image_built": False,
            "package_built": False,
            "publication_performed": False,
            "signed": False,
            "provenance_attestation_created": False,
            "vulnerability_scan_performed": False,
        },
    }


def _build_documents(repo_root: Path) -> tuple[bytes, bytes, int, str]:
    config = _load_config(repo_root)
    sbom, component_count = _build_sbom(repo_root, config)
    sbom_bytes = _json_bytes(sbom)
    manifest = _build_manifest(repo_root, config, sbom_bytes, component_count)
    manifest_bytes = _json_bytes(manifest)
    return sbom_bytes, manifest_bytes, component_count, manifest["release"]["candidate_id"]


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ReleaseEvidenceError(f"cannot publish generated evidence: {path}") from error


def _generate(repo_root: Path) -> JsonObject:
    sbom_bytes, manifest_bytes, component_count, candidate_id = _build_documents(repo_root)
    _write_atomic(repo_root / SBOM_PATH, sbom_bytes)
    _write_atomic(repo_root / MANIFEST_PATH, manifest_bytes)
    return {
        "candidate_id": candidate_id,
        "component_count": component_count,
        "manifest": MANIFEST_PATH.as_posix(),
        "sbom": SBOM_PATH.as_posix(),
        "schema_version": "runtime-v1-release-evidence-result/v1",
        "status": "generated",
    }


def _verify(repo_root: Path) -> JsonObject:
    sbom_bytes, manifest_bytes, component_count, candidate_id = _build_documents(repo_root)
    expected = ((SBOM_PATH, sbom_bytes), (MANIFEST_PATH, manifest_bytes))
    for relative, payload in expected:
        path = repo_root / relative
        try:
            observed = path.read_bytes()
        except OSError as error:
            raise ReleaseEvidenceError(f"generated evidence is missing: {relative}") from error
        if observed != payload:
            raise ReleaseEvidenceError(f"generated evidence drift: {relative}")
    manifest = cast("JsonObject", json.loads(manifest_bytes))
    return {
        "candidate_id": candidate_id,
        "component_count": component_count,
        "material_count": manifest["source"]["material_count"],
        "schema_version": "runtime-v1-release-evidence-result/v1",
        "status": "pass",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "verify"))
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repo_root = arguments.repo_root.resolve()
    try:
        result = _generate(repo_root) if arguments.action == "generate" else _verify(repo_root)
    except (ReleaseEvidenceError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "error": str(error),
                    "schema_version": "runtime-v1-release-evidence-result/v1",
                    "status": "fail",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
