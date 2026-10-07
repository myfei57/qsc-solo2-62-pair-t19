"""Unified error taxonomy for the console.

Every failed request is expressed with one of the exceptions below. The
classification is the contract with clients:

* field errors  -> the request itself is malformed or a field is invalid
* state errors  -> the request is well formed but illegal in the current state
* conflicts     -> the request collides with another holder or an existing row
* not found     -> the referenced resource does not exist
* retryable     -> a downstream/dependency failure the client may retry

Domain modules raise :class:`DomainError` subclasses (state/conflict/not
found/retryable); the gateway layer raises :class:`FieldInvalid` for malformed
input. Nothing user facing is raised as a bare ``ValueError`` any more: the
server maps legacy ``ValueError`` into the taxonomy during the compatibility
window and records where each one originated so the sources can be removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldViolation:
    """One invalid field in a request."""

    field: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


class ConsoleError(Exception):
    """Base class for every classified console error."""

    status: int = 500
    code: str = "INTERNAL_ERROR"
    default_message: str = "internal error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        violations: list[FieldViolation] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message or self.default_message)
        # Per-instance override is reserved for the legacy ValueError bridge;
        # normal callers always keep the class code.
        if code is not None:
            self.code = code
        self.message = message or self.default_message
        self.violations = violations
        self.retry_after = retry_after

    def envelope(self, request_id: str) -> dict[str, object]:
        """Canonical v2 error body."""

        body: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": isinstance(self, RetryableError),
            "request_id": request_id,
        }
        if self.violations:
            body["violations"] = [v.as_dict() for v in self.violations]
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return body


class FieldInvalid(ConsoleError):
    """Malformed request: bad type, missing field, out of range or bad enum."""

    status = 400
    code = "FIELD_INVALID"
    default_message = "request field is invalid"

    def __init__(self, message: str | None = None, *, violations: list[FieldViolation] | None = None) -> None:
        super().__init__(message, violations=violations)

    @staticmethod
    def one(name: str, code: str, message: str) -> "FieldInvalid":
        return FieldInvalid(
            message,
            violations=[FieldViolation(field=name, code=code, message=message)],
        )


class NotFound(ConsoleError):
    """Referenced resource does not exist."""

    status = 404
    code = "NOT_FOUND"
    default_message = "resource not found"


class StateInvalid(ConsoleError):
    """Request is well formed but illegal for the current state machine state."""

    status = 409
    code = "STATE_INVALID"
    default_message = "operation is not allowed in the current state"


class Conflict(ConsoleError):
    """Request collides with an existing row or another holder."""

    status = 409
    code = "CONFLICT"
    default_message = "request conflicts with current state"


class RetryableError(ConsoleError):
    """Downstream/dependency failure. The client may safely retry.

    Mapped to ``503`` with a ``Retry-After`` header so callers run through the
    retry chain instead of treating this as a permanent 4xx failure.
    """

    status = 503
    code = "DOWNSTREAM_UNAVAILABLE"
    default_message = "downstream dependency is temporarily unavailable"

    def __init__(self, message: str | None = None, *, retry_after: int = 1) -> None:
        super().__init__(message, retry_after=retry_after)


class DomainError(ConsoleError):
    """Backwards compatible alias base for domain modules.

    Domain code historically raised plain ``ValueError``. New domain code raises
    :class:`NotFound`/:class:`StateInvalid`/:class:`Conflict` directly.
    """


@dataclass
class LegacyValueErrorBridge:
    """Collects where bare ValueErrors still escape, for audit and cleanup."""

    seen: list[str] = field(default_factory=list)

    def record(self, origin: str, message: str) -> str:
        key = f"{origin}: {message}"
        if key not in self.seen:
            self.seen.append(key)
        return key
