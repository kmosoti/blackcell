"""Bounded runner for trusted, administrator-allowlisted POSIX commands.

This package is *not* a security or isolation boundary. ``SideEffectClass.READ_ONLY`` is an
administrator-reviewed declaration. Process groups support timeout cleanup but provide no
syscall, filesystem, network, dependency, or PID-namespace containment, and cannot contain a
child that deliberately calls ``setsid()``. See ``LOCAL_PROCESS_V1_ACTIVATION_CONTRACT``.
"""

from blackcell.adapters.execution.local_process.adapter import LocalProcessAdapter
from blackcell.adapters.execution.local_process.configuration import (
    LOCAL_PROCESS_ADAPTER_ID,
    ArgumentBinding,
    ArgumentKind,
    EnvironmentEntry,
    LocalProcessAffordance,
    LocalProcessConfigurationError,
    LocalProcessRegistry,
    local_process_affordance_payload,
)
from blackcell.adapters.execution.local_process.runner import (
    LocalProcessRunner,
    ProcessCompletion,
    ProcessRun,
    StreamCapture,
)

LOCAL_PROCESS_V1_ACTIVATION_CONTRACT = """
Activate local-process/v1 only when an administrator owns and keeps immutable the configured ELF
executable, working directory, allowed roots, and every proposal-selectable path input; has audited
the exact command as read-only and non-daemonizing; and accepts residual TOCTOU, dynamically loaded
dependency, and session-escape risks. The runner provides bounded output, deadlines, fd-pinned
executable/cwd selection, and best-effort process-group cleanup only. It does not provide syscall,
filesystem, network, cgroup, or PID-namespace containment. Any untrusted executable, arguments that
can select executable behavior, daemonizing command, or child capable of setsid() is unsupported
until execution is placed behind the planned rootless Podman boundary with cgroup and PID namespace
controls.
""".strip()

__all__ = [
    "LOCAL_PROCESS_ADAPTER_ID",
    "LOCAL_PROCESS_V1_ACTIVATION_CONTRACT",
    "ArgumentBinding",
    "ArgumentKind",
    "EnvironmentEntry",
    "LocalProcessAdapter",
    "LocalProcessAffordance",
    "LocalProcessConfigurationError",
    "LocalProcessRegistry",
    "LocalProcessRunner",
    "ProcessCompletion",
    "ProcessRun",
    "StreamCapture",
    "local_process_affordance_payload",
]
