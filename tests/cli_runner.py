from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from typing import Any


@dataclass(frozen=True, slots=True)
class CliResult:
    exit_code: int
    stdout: str
    stderr: str
    exception: BaseException | None = None


class CycloptsCliRunner:
    def invoke(
        self,
        app: Any,
        args: list[str],
        *,
        catch_exceptions: bool = True,
    ) -> CliResult:
        stdout = StringIO()
        stderr = StringIO()
        exit_code = 0
        exception: BaseException | None = None
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                app(args, exit_on_error=False, print_error=False)
            except SystemExit as error:
                exit_code = int(error.code or 0)
                exception = error
            except BaseException as error:
                if not catch_exceptions:
                    raise
                exit_code = 1
                exception = error
                stderr.write(str(error))
        return CliResult(
            exit_code=exit_code,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            exception=exception,
        )
