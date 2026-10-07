"""Classified errors and the single error-code registry.

Every rejection the console can emit is declared here exactly once: the
HTTP status, the semantic category, whether a client may retry, and the
fixed description. Console envelopes, the route catalog, and the report
exports all read this registry, so the codes a client sees and the codes
a report shows can never drift apart.

Published codes are append only: never recycle a code, never change its
meaning. Add a new code when the semantics change.
"""

from __future__ import annotations

from dataclasses import dataclass

CATEGORY_FIELD = "field"
CATEGORY_NOT_FOUND = "not_found"
CATEGORY_METHOD = "method"
CATEGORY_STATE = "state"
CATEGORY_CONFLICT = "conflict"
CATEGORY_UPSTREAM = "upstream"
CATEGORY_INTERNAL = "internal"


@dataclass(frozen=True)
class ErrorSpec:
    """The registered meaning of one error code."""

    code: str
    status: int
    category: str
    retryable: bool
    summary: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "status": self.status,
            "category": self.category,
            "retryable": self.retryable,
            "summary": self.summary,
        }


_REGISTRY = (
    ErrorSpec("FIELD_INVALID", 400, CATEGORY_FIELD, False, "a request field failed validation"),
    ErrorSpec("NOT_FOUND", 404, CATEGORY_NOT_FOUND, False, "the referenced entity does not exist"),
    ErrorSpec("ROUTE_NOT_FOUND", 404, CATEGORY_NOT_FOUND, False, "no route serves the requested path"),
    ErrorSpec("METHOD_NOT_ALLOWED", 405, CATEGORY_METHOD, False, "the route does not allow the method"),
    ErrorSpec("STATE_INVALID", 409, CATEGORY_STATE, False, "the entity state rejects the operation"),
    ErrorSpec("CONFLICT", 409, CATEGORY_CONFLICT, False, "the operation conflicts with existing state"),
    ErrorSpec("UPSTREAM_FAILURE", 503, CATEGORY_UPSTREAM, True, "a downstream dependency failed; safe to retry"),
    ErrorSpec("INTERNAL", 500, CATEGORY_INTERNAL, False, "an unexpected internal error occurred"),
)

ERROR_REGISTRY: dict[str, ErrorSpec] = {spec.code: spec for spec in _REGISTRY}


def error_catalog() -> list[dict[str, object]]:
    """Every registered code in stable order, shared by console and reports."""

    return [spec.as_dict() for spec in _REGISTRY]


class ConsoleError(Exception):
    """Base for every classified rejection the console can emit."""

    code = "INTERNAL"

    def __init__(self, detail: str = "", *, field: str = "") -> None:
        super().__init__(detail or self.spec().summary)
        self.detail = detail or self.spec().summary
        self.field = field

    @classmethod
    def spec(cls) -> ErrorSpec:
        return ERROR_REGISTRY[cls.code]


class FieldError(ConsoleError, ValueError):
    """A request field is missing, malformed, or out of range.

    Also a :class:`ValueError` so existing internal callers that catch
    validation failures keep working during the migration.
    """

    code = "FIELD_INVALID"


class NotFoundError(ConsoleError, ValueError):
    """The referenced entity does not exist."""

    code = "NOT_FOUND"


class StateError(ConsoleError, ValueError):
    """The entity is in a state that rejects the operation."""

    code = "STATE_INVALID"


class ConflictError(ConsoleError, ValueError):
    """The operation conflicts with state another actor created."""

    code = "CONFLICT"


class RouteNotFoundError(ConsoleError):
    """No route serves the requested path."""

    code = "ROUTE_NOT_FOUND"


class MethodNotAllowedError(ConsoleError):
    """The path exists but does not allow the request method."""

    code = "METHOD_NOT_ALLOWED"


class UpstreamError(ConsoleError):
    """A downstream dependency failed; the caller may retry the request."""

    code = "UPSTREAM_FAILURE"

    def __init__(self, detail: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(detail)
        if cause is not None:
            self.__cause__ = cause
