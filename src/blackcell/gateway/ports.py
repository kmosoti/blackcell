from collections.abc import Mapping, Set
from datetime import datetime
from typing import Protocol

from blackcell.gateway.models import (
    AdapterResult,
    GatewayAuditRecord,
    ModelCapability,
    ModelRequest,
)


class ModelAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    @property
    def capabilities(self) -> Set[ModelCapability]: ...

    @property
    def local(self) -> bool: ...

    @property
    def deterministic(self) -> bool: ...

    def invoke(self, request: ModelRequest, *, model_id: str) -> AdapterResult: ...


class GatewayAuditSink(Protocol):
    def record(self, record: GatewayAuditRecord) -> None: ...


class Clock(Protocol):
    def __call__(self) -> datetime: ...


type AdapterRegistry = Mapping[str, ModelAdapter]
