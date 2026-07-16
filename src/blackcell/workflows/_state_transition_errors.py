from __future__ import annotations


class StateTransitionBindingError(ValueError):
    """Immutable run evidence cannot prove the requested transition command."""


class StateTransitionNotReady(RuntimeError):
    """The run has not yet committed the evidence required for transition acceptance."""


__all__ = ["StateTransitionBindingError", "StateTransitionNotReady"]
