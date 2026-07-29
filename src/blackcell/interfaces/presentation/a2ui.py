"""Pure compatibility projection for BlackCell's closed A2UI-compatible subset."""

from __future__ import annotations

from pydantic import JsonValue

from blackcell.interfaces.presentation.models import PresentationSurface


def export_a2ui(surface: PresentationSurface) -> dict[str, JsonValue]:
    """Export data-separated, stable-ID components without accepting agent-authored actions."""

    components: list[JsonValue] = []
    data: dict[str, JsonValue] = {}
    for component in surface.components:
        payload = component.model_dump(mode="json")
        component_id = str(payload.pop("component_id"))
        kind = str(payload.pop("kind"))
        data[component_id] = payload
        components.append(
            {
                "id": component_id,
                "component": kind,
                "dataBinding": f"/{component_id}",
            }
        )
    return {
        "schemaVersion": "a2ui-compatible/v1",
        "surfaceId": surface.surface_id,
        "revision": surface.revision.number,
        "components": components,
        "data": data,
    }


__all__ = ["export_a2ui"]
