import hashlib
import json
from dataclasses import asdict
from enum import Enum
from typing import Any

from blackcell.control_plane.models import IssuePlan, PlanContract

ISSUE_KEY_MARKER_PREFIX = "<!-- blackcell:issue-key "
CONTRACT_DIGEST_MARKER_PREFIX = "<!-- blackcell:contract-digest "
PR_ISSUE_KEY_MARKER_PREFIX = "<!-- blackcell:pr-issue-key "
PR_DIGEST_MARKER_PREFIX = "<!-- blackcell:pr-digest "
PRIOR_CONTEXT_START = "<!-- blackcell:prior-context-start -->"
PRIOR_CONTEXT_END = "<!-- blackcell:prior-context-end -->"


def issue_contract_digest(contract: PlanContract, issue: IssuePlan) -> str:
    payload = {
        "issue": _jsonable(asdict(issue)),
        "global_policy": _jsonable(asdict(contract.global_policy)),
        "pr_policy": _jsonable(asdict(contract.pr_policy)),
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def issue_body_digest(body: str) -> str:
    return _sha256(body)


def pull_request_body_digest(body: str) -> str:
    return _sha256(body)


def render_issue_body(
    contract: PlanContract,
    issue: IssuePlan,
    *,
    prior_remote_body: str | None = None,
) -> str:
    contract_digest = issue_contract_digest(contract, issue)
    sections = [
        ISSUE_KEY_MARKER_PREFIX + issue.key + " -->",
        CONTRACT_DIGEST_MARKER_PREFIX + contract_digest + " -->",
        "",
        "## BlackCell contract",
        "",
        f"- Key: {issue.key}",
        f"- Type: {issue.kind.value}",
        f"- Status: {issue.status.value}",
        f"- Priority: {issue.priority.value}",
        f"- Complexity: {issue.complexity.value}",
        f"- Epic: {issue.epic or 'None'}",
        f"- Milestone: {issue.milestone or 'None'}",
        "",
        "## Scope",
        "",
        *_bullets(issue.scope),
        "",
        "## Context",
        "",
        *_bullets(issue.context),
        "",
        "## Change Spec",
        "",
        *_bullets(issue.change_spec),
        "",
        "## Acceptance Criteria",
        "",
        *_bullets((*contract.global_policy.acceptance_criteria, *issue.acceptance_criteria)),
        "",
        "## Definition of Ready",
        "",
        *_bullets((*contract.global_policy.definition_of_ready, *issue.definition_of_ready)),
        "",
        "## Definition of Done",
        "",
        *_bullets((*contract.global_policy.definition_of_done, *issue.definition_of_done)),
        "",
        "## Dependencies",
        "",
        *_bullets(issue.depends_on),
        "",
        "## Areas of Responsibility",
        "",
        *_bullets(issue.areas_of_responsibility),
    ]

    if prior_remote_body and prior_remote_body.strip():
        sections.extend(
            [
                "",
                "## Prior remote context",
                "",
                "The following content existed before BlackCell adopted this issue.",
                "",
                PRIOR_CONTEXT_START,
                prior_remote_body.strip(),
                PRIOR_CONTEXT_END,
            ]
        )

    return "\n".join(sections).rstrip() + "\n"


def render_pull_request_body(
    issue: IssuePlan,
    *,
    issue_number: int | None,
    head_ref_name: str,
) -> str:
    body_without_digest = _render_pull_request_body(
        issue,
        issue_number=issue_number,
        head_ref_name=head_ref_name,
        digest=None,
    )
    digest = pull_request_body_digest(body_without_digest)
    return _render_pull_request_body(
        issue,
        issue_number=issue_number,
        head_ref_name=head_ref_name,
        digest=digest,
    )


def has_blackcell_issue_marker(body: str, issue_key: str) -> bool:
    return ISSUE_KEY_MARKER_PREFIX + issue_key + " -->" in body


def has_blackcell_pull_request_marker(body: str, issue_key: str) -> bool:
    return PR_ISSUE_KEY_MARKER_PREFIX + issue_key + " -->" in body


def extract_contract_digest(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith(CONTRACT_DIGEST_MARKER_PREFIX) and line.endswith(" -->"):
            return line.removeprefix(CONTRACT_DIGEST_MARKER_PREFIX).removesuffix(" -->")
    return None


def extract_prior_remote_body(body: str) -> str | None:
    start = body.find(PRIOR_CONTEXT_START)
    end = body.find(PRIOR_CONTEXT_END)
    if start == -1 or end == -1 or end < start:
        return None
    return body[start + len(PRIOR_CONTEXT_START) : end].strip()


def _render_pull_request_body(
    issue: IssuePlan,
    *,
    issue_number: int | None,
    head_ref_name: str,
    digest: str | None,
) -> str:
    related_issue = f"#{issue_number}" if issue_number is not None else "not materialized"
    sections = [
        PR_ISSUE_KEY_MARKER_PREFIX + issue.key + " -->",
        PR_DIGEST_MARKER_PREFIX + (digest or "pending") + " -->",
        "",
        "## BlackCell PR",
        "",
        f"- Issue key: {issue.key}",
        f"- Related issue: {related_issue}",
        f"- Branch: {head_ref_name}",
        f"- Status: {issue.status.value}",
        "",
        "## Change Spec",
        "",
        *_bullets(issue.change_spec),
        "",
        "## Acceptance Criteria",
        "",
        *_bullets(issue.acceptance_criteria),
    ]
    return "\n".join(sections).rstrip() + "\n"


def _bullets(values: tuple[str, ...]) -> list[str]:
    if not values:
        return ["- None"]
    return [f"- {value}" for value in values]


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value
