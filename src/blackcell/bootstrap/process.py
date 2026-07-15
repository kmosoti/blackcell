from __future__ import annotations

import json
import signal
import sys
from collections.abc import Sequence
from threading import Event
from types import FrameType

from blackcell.adapters.recovery import (
    LocalRecoveryService,
    RecoveryBundleInfo,
    RecoveryError,
    RestoreInfo,
)
from blackcell.bootstrap.granian import GranianServer
from blackcell.bootstrap.worker import RuntimeWorker
from blackcell.config import (
    ProcessConfigError,
    RecoveryConfigError,
    RuntimeProcessConfig,
    RuntimeRecoveryConfig,
    SecurityConfigError,
)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ("recovery",):
        return _recovery(arguments[1:])
    if arguments not in {("api",), ("worker",), ("worker", "--once")}:
        return _failure("invalid-command", exit_code=2)
    try:
        if arguments == ("api",):
            config = RuntimeProcessConfig.from_environment()
            GranianServer(config).serve()
            return 0
        return _serve_worker(once=arguments == ("worker", "--once"))
    except (ProcessConfigError, SecurityConfigError) as error:
        return _failure(error.code.value)
    except KeyboardInterrupt:
        return 130
    except LookupError, OSError, RuntimeError, TypeError, ValueError:
        return _failure("runtime-startup-failed")


def _serve_worker(*, once: bool) -> int:
    previous: dict[signal.Signals, signal._HANDLER] = {}
    stop_event = Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop_event.set()

    for kind in (signal.SIGINT, signal.SIGTERM):
        previous[kind] = signal.getsignal(kind)
        signal.signal(kind, request_stop)
    try:
        config = RuntimeProcessConfig.from_environment()
        worker = RuntimeWorker.from_config(config, stop_event=stop_event)
        return worker.serve(once=once)
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)


def _recovery(arguments: tuple[str, ...]) -> int:
    try:
        if arguments == ("backup",):
            config = RuntimeRecoveryConfig.from_environment()
            info = LocalRecoveryService(config.paths).create_backup(
                retention_count=config.retention_count
            )
            return _success(_bundle_payload("backup", info))
        if arguments == ("list",):
            config = RuntimeRecoveryConfig.from_environment()
            backups = LocalRecoveryService(config.paths).list_backups()
            return _success(
                {
                    "schema_version": "blackcell-recovery-list/v1",
                    "operation": "list",
                    "backups": [_bundle_fields(item) for item in backups],
                }
            )
        if len(arguments) == 2 and arguments[0] == "verify":
            info = LocalRecoveryService().verify_bundle(arguments[1])
            return _success(_bundle_payload("verify", info))
        if len(arguments) == 3 and arguments[0] == "restore":
            info = LocalRecoveryService().restore_bundle(arguments[1], arguments[2])
            return _success(_restore_payload(info))
        return _failure("invalid-command", exit_code=2)
    except (RecoveryConfigError, RecoveryError, SecurityConfigError) as error:
        return _failure(error.code.value)
    except OSError, RuntimeError, TypeError, ValueError:
        return _failure("recovery-failed")


def _bundle_payload(operation: str, info: RecoveryBundleInfo) -> dict[str, object]:
    return {
        "schema_version": "blackcell-recovery-result/v1",
        "operation": operation,
        **_bundle_fields(info),
    }


def _bundle_fields(info: RecoveryBundleInfo) -> dict[str, object]:
    return {
        "backup_id": info.backup_id,
        "bundle_path": str(info.bundle_path),
        "created_at": info.created_at.isoformat(),
        "database_digest": info.database_digest,
        "database_bytes": info.database_bytes,
        "database_schema_version": info.schema_version,
        "event_highwater": info.event_highwater,
        "artifact_count": info.artifact_count,
        "artifact_bytes": info.artifact_bytes,
    }


def _restore_payload(info: RestoreInfo) -> dict[str, object]:
    return {
        "schema_version": "blackcell-recovery-result/v1",
        "operation": "restore",
        "target_path": str(info.target_path),
        "backup_id": info.backup_id,
        "event_highwater": info.event_highwater,
        "artifact_count": info.artifact_count,
    }


def _success(payload: dict[str, object]) -> int:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def _failure(code: str, *, exit_code: int = 1) -> int:
    sys.stderr.write(json.dumps({"error": {"code": code}}, sort_keys=True) + "\n")
    return exit_code


__all__ = ["main"]
