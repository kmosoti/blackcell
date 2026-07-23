"""Foreground supervisor for the existing runtime API and worker components."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from threading import Event
from typing import Protocol

from blackcell.bootstrap.alpha_process import validate_alpha_worker_runtime_config
from blackcell.bootstrap.alpha_review_process import (
    validate_alpha_review_worker_runtime_config,
)
from blackcell.bootstrap.alpha_verify_process import (
    validate_alpha_verify_worker_runtime_config,
)
from blackcell.config import RuntimeProcessConfig

_POLL_SECONDS = 0.05
_FORCED_WAIT_SECONDS = 5.0


class ManagedRuntimeProcess(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


RuntimeProcessFactory = Callable[..., ManagedRuntimeProcess]


class RuntimeDaemon:
    """Keep the API and optional alpha worker in one foreground lifecycle."""

    def __init__(
        self,
        repository_root: Path,
        *,
        graceful_timeout_seconds: float,
        stop_event: Event | None = None,
        process_factory: RuntimeProcessFactory = subprocess.Popen,
        command_prefix: Sequence[str] | None = None,
        components: Sequence[str] = ("api",),
        environment: Mapping[str, str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        if not isinstance(repository_root, Path) or not repository_root.is_dir():
            raise ValueError("daemon repository root must be an existing directory")
        if (
            isinstance(graceful_timeout_seconds, bool)
            or not isinstance(graceful_timeout_seconds, int | float)
            or not math.isfinite(graceful_timeout_seconds)
            or not 0 < graceful_timeout_seconds <= 300
        ):
            raise ValueError("daemon graceful timeout must be between 0 and 300 seconds")
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, int | float)
            or not math.isfinite(poll_seconds)
            or not 0 < poll_seconds <= 1
        ):
            raise ValueError("daemon poll interval must be between 0 and 1 second")
        prefix = tuple(command_prefix or (sys.executable, "-m", "blackcell.bootstrap.process"))
        if not prefix or not all(
            isinstance(token, str) and token and "\x00" not in token for token in prefix
        ):
            raise ValueError("daemon command prefix is invalid")
        component_names = tuple(components)
        if (
            not component_names
            or component_names[0] != "api"
            or any(
                item
                not in {
                    "api",
                    "alpha-worker",
                    "alpha-review-worker",
                    "alpha-verify-worker",
                }
                for item in component_names
            )
            or len(set(component_names)) != len(component_names)
        ):
            raise ValueError("daemon components are invalid")
        values = dict(os.environ if environment is None else environment)
        if not all(
            isinstance(key, str)
            and key
            and "\x00" not in key
            and isinstance(value, str)
            and "\x00" not in value
            for key, value in values.items()
        ):
            raise ValueError("daemon environment is invalid")

        self._repository_root = repository_root
        self._graceful_timeout_seconds = float(graceful_timeout_seconds)
        self._stop_event = stop_event or Event()
        self._process_factory = process_factory
        self._command_prefix = prefix
        self._components = component_names
        self._environment = values
        self._monotonic = monotonic
        self._poll_seconds = float(poll_seconds)

    @classmethod
    def from_config(
        cls,
        config: RuntimeProcessConfig,
        *,
        stop_event: Event | None = None,
        environment: Mapping[str, str] | None = None,
        process_factory: RuntimeProcessFactory = subprocess.Popen,
        command_prefix: Sequence[str] | None = None,
    ) -> RuntimeDaemon:
        values = dict(os.environ if environment is None else environment)
        components = ["api"]
        if config.alpha_worker is not None:
            validate_alpha_worker_runtime_config(config, environment=values)
            components.append("alpha-worker")
        if config.alpha_review_worker is not None:
            validate_alpha_review_worker_runtime_config(config, environment=values)
            components.append("alpha-review-worker")
        if config.alpha_verify_worker is not None:
            validate_alpha_verify_worker_runtime_config(config)
            components.append("alpha-verify-worker")
        return cls(
            config.repository_root,
            graceful_timeout_seconds=config.graceful_timeout_seconds,
            stop_event=stop_event,
            process_factory=process_factory,
            command_prefix=command_prefix,
            components=tuple(components),
            environment=values,
        )

    def serve(self) -> int:
        children: list[ManagedRuntimeProcess] = []
        try:
            for component in self._components:
                children.append(self._spawn(component))
            while not self._stop_event.is_set():
                for child in children:
                    return_code = child.poll()
                    if return_code is not None:
                        return _unexpected_component_exit(return_code)
                self._stop_event.wait(self._poll_seconds)
            return 0
        except OSError as error:
            raise RuntimeError("daemon-component-startup-failed") from error
        finally:
            self._stop_children(children)

    def _spawn(self, component: str) -> ManagedRuntimeProcess:
        return self._process_factory(
            [*self._command_prefix, component],
            cwd=self._repository_root,
            env=dict(self._environment),
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            shell=False,
            text=False,
            close_fds=True,
            start_new_session=False,
        )

    def _stop_children(self, children: Sequence[ManagedRuntimeProcess]) -> None:
        running = tuple(child for child in children if child.poll() is None)
        for child in running:
            with suppress(OSError):
                child.terminate()

        deadline = self._monotonic() + self._graceful_timeout_seconds
        stale: list[ManagedRuntimeProcess] = []
        for child in running:
            remaining = max(0.0, deadline - self._monotonic())
            try:
                child.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                stale.append(child)
            except OSError:
                stale.append(child)
        for child in stale:
            try:
                child.kill()
                child.wait(timeout=_FORCED_WAIT_SECONDS)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise RuntimeError("daemon-component-cleanup-failed") from error


def _unexpected_component_exit(return_code: int) -> int:
    if isinstance(return_code, bool) or not isinstance(return_code, int):
        return 1
    if 1 <= return_code <= 255:
        return return_code
    return 1


__all__ = ["ManagedRuntimeProcess", "RuntimeDaemon", "RuntimeProcessFactory"]
