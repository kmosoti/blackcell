"""Strict wire types for Kernform's public agent-mode command envelope."""

from __future__ import annotations

from typing import Literal

from blackcell.interfaces.http.contracts import StrictStruct

KernformWireStatus = Literal["success", "failure", "refused"]


class KernformWireDiagnostic(StrictStruct, frozen=True):
    id: str
    severity: Literal["info", "warning", "error"]
    message: str
    context: dict[str, object]


class KernformWireArtifact(StrictStruct, frozen=True):
    kind: str
    path: str
    hash: str | None


class KernformWireEnvelope(StrictStruct, frozen=True):
    schema: Literal["kernform.command/v1"]
    command: str
    status: KernformWireStatus
    exit_code: int
    result: object
    diagnostics: tuple[KernformWireDiagnostic, ...]
    artifacts: tuple[KernformWireArtifact, ...]


class KernformWireCheckSet(StrictStruct, frozen=True):
    architecture: bool
    boundary: bool
    environment: bool
    git: bool
    state: bool
    testing: bool
    versions: bool


class KernformWireRequirements(StrictStruct, frozen=True):
    conformance: tuple[str, ...]
    tests: tuple[str, ...]


class KernformWireCheckResult(StrictStruct, frozen=True):
    catalog_hash: str
    checks: KernformWireCheckSet
    conformant: bool
    files_checked: int
    mode: Literal["managed-project", "source-repository"]
    requirements: KernformWireRequirements


class KernformWireInitResult(StrictStruct, frozen=True):
    evidence_path: str
    operation_count: int
    plan_id: str
    state_path: str


__all__ = [
    "KernformWireArtifact",
    "KernformWireCheckResult",
    "KernformWireCheckSet",
    "KernformWireDiagnostic",
    "KernformWireEnvelope",
    "KernformWireInitResult",
    "KernformWireRequirements",
    "KernformWireStatus",
]
