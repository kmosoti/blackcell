from __future__ import annotations

import json
import signal
import sys
from collections.abc import Sequence
from threading import Event
from types import FrameType

from blackcell.bootstrap.granian import GranianServer
from blackcell.bootstrap.worker import RuntimeWorker
from blackcell.config import ProcessConfigError, RuntimeProcessConfig, SecurityConfigError


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
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


def _failure(code: str, *, exit_code: int = 1) -> int:
    sys.stderr.write(json.dumps({"error": {"code": code}}, sort_keys=True) + "\n")
    return exit_code


__all__ = ["main"]
