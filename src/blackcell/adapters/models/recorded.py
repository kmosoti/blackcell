from __future__ import annotations

from collections.abc import Mapping, Set

from blackcell.gateway import AdapterResult, ModelCapability, ModelRequest
from blackcell.kernel import JsonValue


class RecordedModelAdapter:
    def __init__(
        self,
        adapter_id: str,
        recordings: Mapping[tuple[str, str], Mapping[str, JsonValue]],
        *,
        capabilities: Set[ModelCapability],
        local: bool = True,
    ) -> None:
        self._adapter_id = adapter_id
        self._recordings = dict(recordings)
        self._capabilities = frozenset(capabilities)
        self._local = local

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    @property
    def capabilities(self) -> Set[ModelCapability]:
        return self._capabilities

    @property
    def local(self) -> bool:
        return self._local

    @property
    def deterministic(self) -> bool:
        return True

    def invoke(self, request: ModelRequest, *, model_id: str) -> AdapterResult:
        try:
            output = self._recordings[(model_id, request.request_id)]
        except KeyError as error:
            raise LookupError(
                f"no recording for model {model_id!r} and request {request.request_id!r}"
            ) from error
        return AdapterResult(
            output,
            input_tokens=request.estimated_input_tokens,
            output_tokens=1,
            latency_ms=0,
            cost_microusd=0,
            deterministic=True,
        )
