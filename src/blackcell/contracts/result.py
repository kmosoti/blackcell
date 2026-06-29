"""Structured command and SDK results."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from blackcell.contracts.errors import BlackcellError, ExitClass


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    recovery: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "error", "pending"]
    exit_class: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: ErrorDetail | None = None

    @classmethod
    def ok(cls, data: dict[str, Any] | None = None) -> ResultEnvelope:
        return cls(status="ok", exit_class=ExitClass.OK.name.lower(), data=data or {})

    @classmethod
    def pending(
        cls, code: str, message: str, recovery: str, data: dict[str, Any] | None = None
    ) -> ResultEnvelope:
        return cls(
            status="pending",
            exit_class=ExitClass.PENDING.name.lower(),
            data=data or {},
            error=ErrorDetail(code=code, message=message, recovery=recovery),
        )

    @classmethod
    def from_error(cls, error: BlackcellError) -> ResultEnvelope:
        return cls(
            status="error",
            exit_class=error.exit_class.name.lower(),
            error=ErrorDetail(
                code=error.code,
                message=error.message,
                recovery=error.recovery,
                details=error.details,
            ),
        )
