"""Closed content-addressed artifact contracts for execution execution outcomes."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

from blackcell.kernel import ArtifactRef, JsonInput
from blackcell.kernel._json import json_digest

NODE_OUTCOME_SCHEMA = "node-outcome/v2"

CONTEXT_MEDIA_TYPE = "application/vnd.blackcell.change-context+json"
PROPOSAL_MEDIA_TYPE = "application/vnd.blackcell.change-proposal+json"
PROVIDER_MEDIA_TYPE = "application/vnd.blackcell.change-provider-result+json"
EFFECT_MEDIA_TYPE = "application/vnd.blackcell.text-change-result+json"
ACCEPTANCE_COMMAND_MEDIA_TYPE = "application/vnd.blackcell.acceptance-command+json"
ACCEPTANCE_RESULT_MEDIA_TYPE = "application/vnd.blackcell.acceptance-result+json"
OUTCOME_MEDIA_TYPE = "application/vnd.blackcell.node-outcome+json"
REVIEW_CONTEXT_MEDIA_TYPE = "application/vnd.blackcell.review-context+json"
REVIEW_PROPOSAL_MEDIA_TYPE = "application/vnd.blackcell.review-proposal+json"
REVIEW_PROVIDER_MEDIA_TYPE = "application/vnd.blackcell.review-provider-result+json"
ADMITTED_REVIEW_MEDIA_TYPE = "application/vnd.blackcell.admitted-review+json"
VERIFICATION_REPORT_MEDIA_TYPE = "application/vnd.blackcell.verification-report+json"

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAX_MEDIA_TYPE_CHARS = 256
_MAX_ENCODING_CHARS = 64
_MAX_ARTIFACT_BYTES = 2**63 - 1


class ExecutionArtifactContractFailureCode(StrEnum):
    INVALID_OUTCOME = "invalid-node-outcome"
    UNSUPPORTED_OUTCOME_SCHEMA = "unsupported-node-outcome-schema"


class ExecutionArtifactContractError(ValueError):
    """Content-free indication that an execution artifact contract is invalid."""

    def __init__(self, code: ExecutionArtifactContractFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ExecutionArtifactLink:
    digest: str
    size_bytes: int
    media_type: str
    encoding: str | None

    def __post_init__(self) -> None:
        if (
            _DIGEST.fullmatch(self.digest) is None
            or isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= _MAX_ARTIFACT_BYTES
            or not isinstance(self.media_type, str)
            or not self.media_type
            or len(self.media_type) > _MAX_MEDIA_TYPE_CHARS
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in self.media_type)
            or (
                self.encoding is not None
                and (
                    not isinstance(self.encoding, str)
                    or not self.encoding
                    or len(self.encoding) > _MAX_ENCODING_CHARS
                    or any(
                        ord(character) < 0x21 or ord(character) > 0x7E
                        for character in self.encoding
                    )
                )
            )
        ):
            raise ExecutionArtifactContractError(
                ExecutionArtifactContractFailureCode.INVALID_OUTCOME
            )

    @classmethod
    def from_reference(cls, reference: ArtifactRef) -> ExecutionArtifactLink:
        if not isinstance(reference, ArtifactRef):
            raise ExecutionArtifactContractError(
                ExecutionArtifactContractFailureCode.INVALID_OUTCOME
            )
        return cls(
            reference.digest,
            reference.size_bytes,
            reference.media_type,
            reference.encoding,
        )


@dataclass(frozen=True, slots=True)
class CheckArtifacts:
    check_id: str
    command_digest: str
    result_digest: str
    passed: bool
    command: ExecutionArtifactLink
    result: ExecutionArtifactLink
    stdout: ExecutionArtifactLink
    stderr: ExecutionArtifactLink

    def __post_init__(self) -> None:
        if (
            _IDENTIFIER.fullmatch(self.check_id) is None
            or _DIGEST.fullmatch(self.command_digest) is None
            or _DIGEST.fullmatch(self.result_digest) is None
            or not isinstance(self.passed, bool)
            or not all(
                isinstance(link, ExecutionArtifactLink)
                for link in (self.command, self.result, self.stdout, self.stderr)
            )
            or self.command.digest != self.command_digest
            or self.result.digest != self.result_digest
        ):
            raise ExecutionArtifactContractError(
                ExecutionArtifactContractFailureCode.INVALID_OUTCOME
            )


@dataclass(frozen=True, slots=True)
class NodeOutcomeManifest:
    run_id: str
    node_id: str
    attempt: int
    fencing_token: int
    lease_digest: str
    worktree_spec_digest: str
    base_commit: str
    head_commit: str | None
    repository_write: bool
    status: Literal["succeeded", "failed", "canceled"]
    failure_code: str | None
    context_artifact: ExecutionArtifactLink | None
    proposal_artifact: ExecutionArtifactLink | None
    provider_artifact: ExecutionArtifactLink | None
    effect_artifact: ExecutionArtifactLink | None
    checks: tuple[CheckArtifacts, ...]
    schema_version: Literal["node-outcome/v2"] = NODE_OUTCOME_SCHEMA

    def __post_init__(self) -> None:
        artifacts = (
            self.context_artifact,
            self.proposal_artifact,
            self.provider_artifact,
            self.effect_artifact,
        )
        if (
            self.schema_version != NODE_OUTCOME_SCHEMA
            or _IDENTIFIER.fullmatch(self.run_id) is None
            or _IDENTIFIER.fullmatch(self.node_id) is None
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in (self.attempt, self.fencing_token)
            )
            or _DIGEST.fullmatch(self.lease_digest) is None
            or _DIGEST.fullmatch(self.worktree_spec_digest) is None
            or _COMMIT.fullmatch(self.base_commit) is None
            or (self.head_commit is not None and _COMMIT.fullmatch(self.head_commit) is None)
            or not isinstance(self.repository_write, bool)
            or self.status not in {"succeeded", "failed", "canceled"}
            or (self.failure_code is not None and _IDENTIFIER.fullmatch(self.failure_code) is None)
            or not isinstance(self.checks, tuple)
            or not all(isinstance(check, CheckArtifacts) for check in self.checks)
            or len({check.check_id for check in self.checks}) != len(self.checks)
            or any(
                link is not None and not isinstance(link, ExecutionArtifactLink)
                for link in artifacts
            )
        ):
            raise ExecutionArtifactContractError(
                ExecutionArtifactContractFailureCode.INVALID_OUTCOME
            )
        if (
            (self.proposal_artifact is not None and self.context_artifact is None)
            or (
                self.provider_artifact is not None
                and (self.context_artifact is None or self.proposal_artifact is None)
            )
            or (
                self.effect_artifact is not None
                and any(
                    link is None
                    for link in (
                        self.context_artifact,
                        self.proposal_artifact,
                        self.provider_artifact,
                    )
                )
            )
        ):
            raise ExecutionArtifactContractError(
                ExecutionArtifactContractFailureCode.INVALID_OUTCOME
            )
        if self.status == "succeeded":
            if (
                self.failure_code is not None
                or self.head_commit is None
                or not self.checks
                or not all(check.passed for check in self.checks)
            ):
                raise ExecutionArtifactContractError(
                    ExecutionArtifactContractFailureCode.INVALID_OUTCOME
                )
            if self.repository_write != all(link is not None for link in artifacts):
                raise ExecutionArtifactContractError(
                    ExecutionArtifactContractFailureCode.INVALID_OUTCOME
                )
        elif (self.status == "failed" and not self.failure_code) or (
            self.status == "canceled" and self.failure_code is not None
        ):
            raise ExecutionArtifactContractError(
                ExecutionArtifactContractFailureCode.INVALID_OUTCOME
            )
        if not self.repository_write and any(link is not None for link in artifacts):
            raise ExecutionArtifactContractError(
                ExecutionArtifactContractFailureCode.INVALID_OUTCOME
            )

    @property
    def digest(self) -> str:
        return json_digest(node_outcome_payload(self))


def node_outcome_payload(manifest: NodeOutcomeManifest) -> dict[str, JsonInput]:
    if not isinstance(manifest, NodeOutcomeManifest):
        raise ExecutionArtifactContractError(ExecutionArtifactContractFailureCode.INVALID_OUTCOME)
    return {
        "schema_version": manifest.schema_version,
        "run_id": manifest.run_id,
        "node_id": manifest.node_id,
        "attempt": manifest.attempt,
        "fencing_token": manifest.fencing_token,
        "lease_digest": manifest.lease_digest,
        "worktree_spec_digest": manifest.worktree_spec_digest,
        "base_commit": manifest.base_commit,
        "head_commit": manifest.head_commit,
        "repository_write": manifest.repository_write,
        "status": manifest.status,
        "failure_code": manifest.failure_code,
        "context_artifact": _optional_link_payload(manifest.context_artifact),
        "proposal_artifact": _optional_link_payload(manifest.proposal_artifact),
        "provider_artifact": _optional_link_payload(manifest.provider_artifact),
        "effect_artifact": _optional_link_payload(manifest.effect_artifact),
        "checks": [_check_payload(check) for check in manifest.checks],
    }


def node_outcome_from_mapping(value: Mapping[str, object]) -> NodeOutcomeManifest:
    expected = {
        "schema_version",
        "run_id",
        "node_id",
        "attempt",
        "fencing_token",
        "lease_digest",
        "worktree_spec_digest",
        "base_commit",
        "head_commit",
        "repository_write",
        "status",
        "failure_code",
        "context_artifact",
        "proposal_artifact",
        "provider_artifact",
        "effect_artifact",
        "checks",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ExecutionArtifactContractError(ExecutionArtifactContractFailureCode.INVALID_OUTCOME)
    if value.get("schema_version") != NODE_OUTCOME_SCHEMA:
        raise ExecutionArtifactContractError(
            ExecutionArtifactContractFailureCode.UNSUPPORTED_OUTCOME_SCHEMA
        )
    checks_value = _sequence(value.get("checks"))
    checks = tuple(_check_from_mapping(item) for item in checks_value)
    run_id = value.get("run_id")
    node_id = value.get("node_id")
    attempt = value.get("attempt")
    fencing_token = value.get("fencing_token")
    lease_digest = value.get("lease_digest")
    spec_digest = value.get("worktree_spec_digest")
    base_commit = value.get("base_commit")
    head_commit = value.get("head_commit")
    repository_write = value.get("repository_write")
    status = value.get("status")
    failure_code = value.get("failure_code")
    if (
        not isinstance(run_id, str)
        or not isinstance(node_id, str)
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or isinstance(fencing_token, bool)
        or not isinstance(fencing_token, int)
        or not isinstance(lease_digest, str)
        or not isinstance(spec_digest, str)
        or not isinstance(base_commit, str)
        or (head_commit is not None and not isinstance(head_commit, str))
        or not isinstance(repository_write, bool)
        or not isinstance(status, str)
        or status not in {"succeeded", "failed", "canceled"}
        or (failure_code is not None and not isinstance(failure_code, str))
    ):
        raise ExecutionArtifactContractError(ExecutionArtifactContractFailureCode.INVALID_OUTCOME)
    return NodeOutcomeManifest(
        run_id=run_id,
        node_id=node_id,
        attempt=attempt,
        fencing_token=fencing_token,
        lease_digest=lease_digest,
        worktree_spec_digest=spec_digest,
        base_commit=base_commit,
        head_commit=head_commit,
        repository_write=repository_write,
        status=cast("Literal['succeeded', 'failed', 'canceled']", status),
        failure_code=failure_code,
        context_artifact=_optional_link_from_mapping(value.get("context_artifact")),
        proposal_artifact=_optional_link_from_mapping(value.get("proposal_artifact")),
        provider_artifact=_optional_link_from_mapping(value.get("provider_artifact")),
        effect_artifact=_optional_link_from_mapping(value.get("effect_artifact")),
        checks=checks,
    )


def _check_payload(check: CheckArtifacts) -> dict[str, JsonInput]:
    return {
        "check_id": check.check_id,
        "command_digest": check.command_digest,
        "result_digest": check.result_digest,
        "passed": check.passed,
        "command_artifact": _link_payload(check.command),
        "result_artifact": _link_payload(check.result),
        "stdout_artifact": _link_payload(check.stdout),
        "stderr_artifact": _link_payload(check.stderr),
    }


def _check_from_mapping(value: object) -> CheckArtifacts:
    expected = {
        "check_id",
        "command_digest",
        "result_digest",
        "passed",
        "command_artifact",
        "result_artifact",
        "stdout_artifact",
        "stderr_artifact",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ExecutionArtifactContractError(ExecutionArtifactContractFailureCode.INVALID_OUTCOME)
    check_id = value.get("check_id")
    command_digest = value.get("command_digest")
    result_digest = value.get("result_digest")
    passed = value.get("passed")
    if (
        not isinstance(check_id, str)
        or not isinstance(command_digest, str)
        or not isinstance(result_digest, str)
        or not isinstance(passed, bool)
    ):
        raise ExecutionArtifactContractError(ExecutionArtifactContractFailureCode.INVALID_OUTCOME)
    return CheckArtifacts(
        check_id=check_id,
        command_digest=command_digest,
        result_digest=result_digest,
        passed=passed,
        command=_link_from_mapping(value.get("command_artifact")),
        result=_link_from_mapping(value.get("result_artifact")),
        stdout=_link_from_mapping(value.get("stdout_artifact")),
        stderr=_link_from_mapping(value.get("stderr_artifact")),
    )


def _link_payload(link: ExecutionArtifactLink) -> dict[str, JsonInput]:
    return {
        "digest": link.digest,
        "size_bytes": link.size_bytes,
        "media_type": link.media_type,
        "encoding": link.encoding,
    }


def _optional_link_payload(link: ExecutionArtifactLink | None) -> dict[str, JsonInput] | None:
    return None if link is None else _link_payload(link)


def _link_from_mapping(value: object) -> ExecutionArtifactLink:
    if not isinstance(value, Mapping) or set(value) != {
        "digest",
        "size_bytes",
        "media_type",
        "encoding",
    }:
        raise ExecutionArtifactContractError(ExecutionArtifactContractFailureCode.INVALID_OUTCOME)
    digest = value.get("digest")
    size_bytes = value.get("size_bytes")
    media_type = value.get("media_type")
    encoding = value.get("encoding")
    if (
        not isinstance(digest, str)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not isinstance(media_type, str)
        or (encoding is not None and not isinstance(encoding, str))
    ):
        raise ExecutionArtifactContractError(ExecutionArtifactContractFailureCode.INVALID_OUTCOME)
    return ExecutionArtifactLink(digest, size_bytes, media_type, encoding)


def _optional_link_from_mapping(value: object) -> ExecutionArtifactLink | None:
    return None if value is None else _link_from_mapping(value)


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ExecutionArtifactContractError(ExecutionArtifactContractFailureCode.INVALID_OUTCOME)
    return cast("Sequence[object]", value)


__all__ = [
    "ACCEPTANCE_COMMAND_MEDIA_TYPE",
    "ACCEPTANCE_RESULT_MEDIA_TYPE",
    "ADMITTED_REVIEW_MEDIA_TYPE",
    "CONTEXT_MEDIA_TYPE",
    "EFFECT_MEDIA_TYPE",
    "NODE_OUTCOME_SCHEMA",
    "OUTCOME_MEDIA_TYPE",
    "PROPOSAL_MEDIA_TYPE",
    "PROVIDER_MEDIA_TYPE",
    "REVIEW_CONTEXT_MEDIA_TYPE",
    "REVIEW_PROPOSAL_MEDIA_TYPE",
    "REVIEW_PROVIDER_MEDIA_TYPE",
    "VERIFICATION_REPORT_MEDIA_TYPE",
    "CheckArtifacts",
    "ExecutionArtifactContractError",
    "ExecutionArtifactContractFailureCode",
    "ExecutionArtifactLink",
    "NodeOutcomeManifest",
    "node_outcome_from_mapping",
    "node_outcome_payload",
]
