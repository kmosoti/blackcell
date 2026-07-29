"""Renderer-neutral presentation contracts and deterministic host projections."""

from blackcell.interfaces.presentation import models as _models
from blackcell.interfaces.presentation.a2ui import export_a2ui
from blackcell.interfaces.presentation.fields import (
    ACTION_BINDINGS,
    REQUEST_FIELD_DISPOSITIONS,
    action_binding,
)
from blackcell.interfaces.presentation.models import *  # noqa: F403
from blackcell.interfaces.presentation.projector import run_surface, workspace_surface
from blackcell.interfaces.presentation.qa import UiScenario, semantic_manifest

__all__ = [
    *_models.__all__,
    "ACTION_BINDINGS",
    "REQUEST_FIELD_DISPOSITIONS",
    "UiScenario",
    "action_binding",
    "export_a2ui",
    "run_surface",
    "semantic_manifest",
    "workspace_surface",
]
