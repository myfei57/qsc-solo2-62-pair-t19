"""Domain level classified errors.

The domain layer cannot depend on the console package, so it owns its base
class here. The console maps every :class:`DomainError` straight onto the
matching console error class (they share the same ``status``/``code``), while
legacy ``ValueError`` raised by older code is bridged at the gateway boundary
during the compatibility window.
"""

from __future__ import annotations


class DomainError(ValueError):
    """Base class for every classified domain error.

    Inherits :class:`ValueError` on purpose: historical callers and tests
    catch ``ValueError`` around domain operations, so classified errors must
    keep satisfying that catch throughout the compatibility window. The
    console maps them by their concrete subclass before falling back to the
    plain ``ValueError`` bridge.
    """

    status: int = 400
    code: str = "FIELD_INVALID"
    default_message: str = "request is invalid"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


class ResourceNotFound(DomainError):
    status = 404
    code = "NOT_FOUND"
    default_message = "resource not found"


class IllegalState(DomainError):
    status = 409
    code = "STATE_INVALID"
    default_message = "operation is not allowed in the current state"


class ResourceConflict(DomainError):
    status = 409
    code = "CONFLICT"
    default_message = "request conflicts with current state"


class FieldMalformed(DomainError):
    """Structural/field validation failure raised inside the domain layer."""

    status = 400
    code = "FIELD_INVALID"
    default_message = "request field is invalid"


class DownstreamUnavailable(DomainError):
    status = 503
    code = "DOWNSTREAM_UNAVAILABLE"
    default_message = "downstream dependency is temporarily unavailable"

    def __init__(self, message: str | None = None, retry_after: int = 1) -> None:
        super().__init__(message)
        self.retry_after = retry_after
