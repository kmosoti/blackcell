from __future__ import annotations

import shutil
from pathlib import Path

from blackcell.runtime.models import DoctorReport, RuntimeAdapter
from blackcell.world.models import WorldSnapshot


def list_runtime_adapters() -> tuple[RuntimeAdapter, ...]:
    opencode_available = _which_runtime("opencode", Path.home() / ".opencode" / "bin" / "opencode")
    return (
        RuntimeAdapter(
            name="dry-run",
            available=True,
            kind="simulated",
            supports_write=False,
            description="Records planned work without dispatching an external agent runtime.",
        ),
        RuntimeAdapter(
            name="opencode",
            available=opencode_available,
            kind="external-agent",
            supports_write=True,
            description="Preferred OpenCode adapter for local project or global agent packs.",
        ),
    )


def doctor_report(snapshot: WorldSnapshot) -> DoctorReport:
    adapters = list_runtime_adapters()
    return DoctorReport(
        repo_root=str(snapshot.repo_root),
        branch=snapshot.branch,
        adapter_count=len(adapters),
        adapters=adapters,
    )


def _which_runtime(name: str, fallback: Path) -> bool:
    return shutil.which(name) is not None or fallback.exists()
