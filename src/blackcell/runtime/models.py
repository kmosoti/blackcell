from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeAdapter:
    name: str
    available: bool
    kind: str
    supports_write: bool
    description: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    repo_root: str
    branch: str | None
    adapter_count: int
    adapters: tuple[RuntimeAdapter, ...]
