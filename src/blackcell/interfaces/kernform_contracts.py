"""Strict wire types for Kernform's public agent-mode command envelope."""

from __future__ import annotations

from typing import Literal

from blackcell.interfaces.http.contracts import StrictStruct

KernformWireStatus = Literal["success", "failure", "refused"]
KernformWireSignature = Literal["sdk", "cli", "api", "interactive-web", "daemon"]


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
    schema: Literal["kernform.command/v2"]
    command: str
    status: KernformWireStatus
    exit_code: int
    result: object
    diagnostics: tuple[KernformWireDiagnostic, ...]
    artifacts: tuple[KernformWireArtifact, ...]


class KernformWireCheckResult(StrictStruct, frozen=True):
    conformant: bool
    mode: Literal["source-repository"] | None = None
    catalog_hash: str | None = None
    files_checked: int | None = None
    legacy_schema: Literal["kernform/v1"] | None = None
    migration_required: bool | None = None
    mapped_signatures: tuple[KernformWireSignature, ...] = ()
    managed_state: bool | None = None


class KernformWireInitResult(StrictStruct, frozen=True):
    operation_count: int
    plan_id: str
    state_path: str


class KernformWirePlanIntent(StrictStruct, frozen=True):
    name: str
    requested_signatures: tuple[KernformWireSignature, ...]
    resolved_signatures: tuple[KernformWireSignature, ...]
    default_signature: KernformWireSignature | None
    capabilities: tuple[str, ...]
    git: bool


class KernformWirePlanCatalog(StrictStruct, frozen=True):
    id: str
    hash: str
    resolved_at: str
    source: str
    versions: dict[str, str]
    images: dict[str, str]


class KernformWireCompileResult(StrictStruct, frozen=True):
    schema: Literal["kernform.plan/v2"]
    plan_id: str
    generator_version: str
    intent: KernformWirePlanIntent
    catalog: KernformWirePlanCatalog
    operations: tuple[dict[str, object], ...]
    diagnostics: tuple[KernformWireDiagnostic, ...]


__all__ = [
    "KernformWireArtifact",
    "KernformWireCheckResult",
    "KernformWireCompileResult",
    "KernformWireDiagnostic",
    "KernformWireEnvelope",
    "KernformWireInitResult",
    "KernformWirePlanCatalog",
    "KernformWirePlanIntent",
    "KernformWireSignature",
    "KernformWireStatus",
]
