from blackcell.control_plane.models import Complexity, IssueStatus, IssueType, Priority, enum_values


def plan_contract_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "BlackCell control-plane planning contract",
        "type": "object",
        "required": ["version", "project", "issues"],
        "additionalProperties": False,
        "properties": {
            "version": {"const": 1},
            "project": {
                "type": "object",
                "required": ["key", "name"],
                "additionalProperties": False,
                "properties": {
                    "key": {"type": "string", "minLength": 1},
                    "name": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                },
            },
            "global": _global_policy_schema(),
            "pr_policy": _pr_policy_schema(),
            "roadmaps": {"type": "array", "items": _roadmap_schema()},
            "epics": {"type": "array", "items": _epic_schema()},
            "milestones": {"type": "array", "items": _milestone_schema()},
            "issues": {"type": "array", "items": _issue_schema()},
            "native_automation": {"type": "array", "items": _native_automation_schema()},
            "agent_workflow": _agent_workflow_schema(),
        },
    }


def _global_policy_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "acceptance_criteria": _string_array(),
            "definition_of_ready": _string_array(),
            "definition_of_done": _string_array(),
        },
    }


def _pr_policy_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "require_issue_link": {"type": "boolean"},
            "required_checks": _string_array(),
            "merge_strategy": {"type": "string", "minLength": 1},
        },
    }


def _roadmap_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["key", "title"],
        "additionalProperties": False,
        "properties": {
            "key": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
            "epics": _string_array(),
        },
    }


def _epic_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["key", "title", "roadmap"],
        "additionalProperties": False,
        "properties": {
            "key": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
            "roadmap": {"type": "string", "minLength": 1},
            "milestones": _string_array(),
        },
    }


def _milestone_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["key", "title", "epic"],
        "additionalProperties": False,
        "properties": {
            "key": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
            "epic": {"type": "string", "minLength": 1},
            "target": {"type": "string", "minLength": 1},
        },
    }


def _issue_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["key", "title", "type", "status", "priority", "complexity"],
        "additionalProperties": False,
        "properties": {
            "key": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
            "type": {"enum": enum_values(IssueType)},
            "status": {"enum": enum_values(IssueStatus)},
            "priority": {"enum": enum_values(Priority)},
            "complexity": {"enum": enum_values(Complexity)},
            "epic": {"type": "string", "minLength": 1},
            "milestone": {"type": "string", "minLength": 1},
            "depends_on": _string_array(),
            "areas_of_responsibility": _string_array(),
            "scope": _string_array(),
            "context": _string_array(),
            "change_spec": _string_array(),
            "acceptance_criteria": _string_array(),
            "definition_of_ready": _string_array(),
            "definition_of_done": _string_array(),
        },
    }


def _native_automation_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["key", "name", "trigger", "action"],
        "additionalProperties": False,
        "properties": {
            "key": {"type": "string", "minLength": 1},
            "name": {"type": "string", "minLength": 1},
            "trigger": {"type": "string", "minLength": 1},
            "action": {"type": "string", "minLength": 1},
            "enabled": {"type": "boolean"},
        },
    }


def _agent_workflow_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["model"],
        "additionalProperties": False,
        "properties": {
            "model": {"type": "string", "minLength": 1},
            "workers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["key", "name"],
                    "additionalProperties": False,
                    "properties": {
                        "key": {"type": "string", "minLength": 1},
                        "name": {"type": "string", "minLength": 1},
                        "model": {"type": "string", "minLength": 1},
                        "owns": _string_array(),
                        "change_spec": _string_array(),
                    },
                },
            },
            "codex_cli": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_threads": {"type": "integer", "minimum": 1},
                    "max_depth": {"type": "integer", "minimum": 0, "maximum": 1},
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "key",
                                "name",
                                "description",
                                "developer_instructions",
                            ],
                            "additionalProperties": False,
                            "properties": {
                                "key": {"type": "string", "minLength": 1},
                                "name": {"type": "string", "minLength": 1},
                                "description": {"type": "string", "minLength": 1},
                                "developer_instructions": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                                "sandbox_mode": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
        },
    }


def _string_array() -> dict[str, object]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
    }
