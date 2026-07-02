from blackcell.control_plane.models import IssuePlan
from blackcell.models import ProjectFieldRef, ProjectItemFieldValueRef, ProjectItemRef
from blackcell.providers.base import ProjectFieldValue


def project_field_specs() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    return (
        ("Status", "SINGLE_SELECT", ("Backlog", "Todo", "In Progress", "Review Required", "Done")),
        ("Priority", "SINGLE_SELECT", ("P0", "P1", "P2", "P3")),
        ("Complexity", "NUMBER", ()),
        ("Type", "SINGLE_SELECT", ("feature", "bug", "refactor", "chore")),
    )


def desired_project_field_values(issue: IssuePlan) -> tuple[tuple[str, str | int], ...]:
    return (
        ("Status", issue.status.value),
        ("Priority", issue.priority.value),
        ("Complexity", issue.complexity.value),
        ("Type", issue.kind.value),
    )


def missing_required_fields(fields: list[ProjectFieldRef]) -> bool:
    field_names = {field.name for field in fields}
    return any(field_name not in field_names for field_name, _, _ in project_field_specs())


def project_field_value(field: ProjectFieldRef, desired_value: str | int) -> ProjectFieldValue:
    if field.data_type == "NUMBER":
        if not isinstance(desired_value, int):
            raise ValueError(f"GitHub Project field {field.name} expected numeric value")
        return ProjectFieldValue(number=float(desired_value))
    if field.data_type == "SINGLE_SELECT":
        option_by_name = {option.name: option for option in field.options}
        option = option_by_name.get(str(desired_value))
        if option is None or option.id is None:
            raise ValueError(f"GitHub Project field {field.name} missing option {desired_value}")
        return ProjectFieldValue(single_select_option_id=option.id)
    raise ValueError(f"unsupported GitHub Project field type for {field.name}: {field.data_type}")


def current_field_value(
    project_item: ProjectItemRef,
    field_id: str,
) -> ProjectItemFieldValueRef | None:
    for value in project_item.field_values:
        if value.field_id == field_id:
            return value
    return None


def field_value_matches(
    current: ProjectItemFieldValueRef | None,
    desired_value: str | int,
) -> bool:
    if current is None:
        return False
    if isinstance(desired_value, int):
        return current.number == float(desired_value)
    return current.option_name == desired_value or current.text == desired_value
