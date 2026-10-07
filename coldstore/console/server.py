"""WSGI application that serves the control console.

This module owns the single error/validation boundary:

1. the registered contract performs structural field validation before the
   handler runs (gateway layer);
2. the handler and the domain layer raise classified errors for state,
   conflict, not-found and retryable conditions;
3. one render path maps every error onto the canonical v2 envelope or the
   legacy ``{"error": ...}`` body, records the outcome in the audit trail and
   emits the right status/headers.

Validation stays *centralised at the gateway for shape*, while *state* checks
stay *in the domain* because only the domain holds the locks. Bare
``ValueError`` still escapes in a few places; during the compatibility window
it is bridged to ``FIELD_INVALID`` and its origin is audited so those call
sites can be found and converted.
"""

from __future__ import annotations

import uuid
from typing import Callable, Iterable

from coldstore.errors import (
    DomainError,
    DownstreamUnavailable,
    FieldMalformed,
    IllegalState,
    ResourceConflict,
    ResourceNotFound,
)
from coldstore.store import Store

from . import handlers
from .contracts import all_contracts, contract_for
from .errors import (
    Conflict,
    ConsoleError,
    FieldInvalid,
    NotFound,
    RetryableError,
    StateInvalid,
)
from .http import (
    Request,
    RequestError,
    Response,
    Router,
    build_request,
    error_response,
    json_response,
)
from .metrics import Metrics
from .routes import register_routes
from .runtime import Runtime

# Domain error class -> console error class. Status and code are identical, so
# the mapping only swaps the type that the console renderer understands.
_DOMAIN_MAP: dict[type[DomainError], type[ConsoleError]] = {
    ResourceNotFound: NotFound,
    IllegalState: StateInvalid,
    ResourceConflict: Conflict,
    FieldMalformed: FieldInvalid,
    DownstreamUnavailable: RetryableError,
}

# Legacy wire status per code. Old clients were built when everything arrived
# as 400 (and failures as 500), so the legacy envelope keeps those statuses.
_LEGACY_STATUS = {
    "FIELD_INVALID": 400,
    "NOT_FOUND": 400,
    "STATE_INVALID": 400,
    "CONFLICT": 400,
    "DOWNSTREAM_UNAVAILABLE": 500,
    "INTERNAL_ERROR": 500,
    "METHOD_NOT_ALLOWED": 405,
    "ROUTE_NOT_FOUND": 404,
}


class Server:
    """Owns the runtime state and dispatches console requests."""

    def __init__(
        self,
        store: Store,
        *,
        legacy_errors_by_default: bool = True,
        register_contracts: bool = True,
    ) -> None:
        self.runtime = Runtime(store)
        self.router = Router()
        self.metrics = Metrics()
        register_routes(self.router, handlers)
        # Compatibility window switch. When False the canonical v2 envelope is
        # the default and legacy is only served on explicit request.
        self.legacy_default = legacy_errors_by_default
        # Routes whose contract has already been written to the audit trail,
        # so restarting the console does not duplicate contract registrations.
        self._registered_contracts: set[str] = set()
        if register_contracts:
            self.register_contracts()

    # ------------------------------------------------------------------
    # contract registration
    # ------------------------------------------------------------------
    def register_contracts(self) -> None:
        """Register every served contract once, into the shared audit trail."""

        auditor = self.runtime.auditor
        known = {
            entry.route
            for entry in auditor.entries()
            if entry.kind == "contract"
        }
        for contract in all_contracts():
            route = f"{contract.method} {contract.path}"
            if route in known or route in self._registered_contracts:
                continue
            auditor.record_contract(route, contract.as_dict())
            self._registered_contracts.add(route)

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    def respond(self, request: Request) -> Response:
        try:
            response = self._route(request)
        except Exception as exc:  # noqa: BLE001 - boundary renders every error
            response = self._render_error(exc, request)
        self.metrics.record(request.method, request.path, response.status)
        return response

    def _route(self, request: Request) -> Response:
        handler = self.router.match(request.method, request.path)
        if handler is None:
            if self.router.allows(request.path):
                raise _MethodNotAllowed(
                    f"method {request.method} not allowed for {request.path}"
                )
            raise _RouteNotFound(f"no route for {request.path}")

        contract = contract_for(request.method, request.path)
        if contract is not None:
            try:
                request = _replace_payload(request, contract.validate(request.payload))
                self.runtime.auditor.record_validation(
                    f"{request.method} {request.path}", True, request_id=request.request_id
                )
            except FieldInvalid as exc:
                self._audit_failure(request, exc)
                raise

        return handler(self, request)

    # ------------------------------------------------------------------
    # error pipeline
    # ------------------------------------------------------------------
    def _classify(self, exc: Exception) -> ConsoleError:
        if isinstance(exc, ConsoleError):
            return exc
        if isinstance(exc, DomainError):
            cls = _DOMAIN_MAP.get(type(exc), FieldInvalid)
            mapped: ConsoleError
            if cls is RetryableError and isinstance(exc, DownstreamUnavailable):
                mapped = RetryableError(exc.message, retry_after=exc.retry_after)
            else:
                mapped = cls(exc.message)
            return mapped
        if isinstance(exc, ValueError):
            # Legacy bridge. Audited via _audit_failure so the remaining bare
            # ValueError sites can be tracked and removed before sunset.
            return FieldInvalid(str(exc))
        return _InternalError("internal error")

    def _render_error(self, exc: Exception, request: Request) -> Response:
        error = self._classify(exc)
        wire_status = error.status
        # Legacy RequestError instances (bad JSON framing, Request field
        # coercion in handlers that still call the old helpers) carry the
        # status they were constructed with; preserve it on the wire.
        if isinstance(exc, RequestError):
            wire_status = exc.status
        self._audit_failure(request, error, wire_status=wire_status)

        if self._legacy_for(request):
            # Routing statuses stay literal; business errors collapse to the
            # old 400/500 statuses legacy clients were built against.
            status = wire_status if error.code in ("METHOD_NOT_ALLOWED", "ROUTE_NOT_FOUND") else _LEGACY_STATUS.get(error.code, wire_status)
            return error_response(status, error.message)

        body = error.envelope(request.request_id)
        headers: tuple[tuple[str, str], ...] = ()
        if error.retry_after is not None:
            headers = (("Retry-After", str(error.retry_after)),)
        headers += (("X-Request-Id", request.request_id),)
        return Response(
            status=wire_status,
            body=json_response(body, wire_status).body,
            content_type="application/json; charset=utf-8",
            extra_headers=headers,
        )

    def _legacy_for(self, request: Request) -> bool:
        if request.wants_v2_errors():
            return False
        if request.wants_legacy_errors():
            return True
        return self.legacy_default

    def _audit_failure(
        self, request: Request, error: ConsoleError, *, wire_status: int | None = None
    ) -> None:
        route = f"{request.method} {request.path}"
        status = wire_status if wire_status is not None else error.status
        if isinstance(error, FieldInvalid) and error.violations:
            self.runtime.auditor.record_validation(
                route,
                False,
                violations=[v.as_dict() for v in error.violations],
                request_id=request.request_id,
            )
        self.runtime.auditor.record_error(
            route,
            status,
            error.code,
            message=error.message,
            retryable=isinstance(error, RetryableError),
            request_id=request.request_id,
        )

    def dispatch(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> Response:
        """Call a route directly, used by the test suite and CLI probes."""

        request = Request(
            method=method.upper(),
            path=path,
            query={},
            payload=dict(payload or {}),
            request_id=uuid.uuid4().hex,
        )
        return self.respond(request)

    def __call__(
        self, environ: dict[str, object], start_response: Callable[..., object]
    ) -> Iterable[bytes]:
        try:
            request = build_request(environ)
        except RequestError as exc:
            # Malformed JSON/framing still enters the same render pipeline so
            # v2 clients receive the classified envelope, not a legacy body.
            headers = {}
            for key, value in environ.items():
                if key.startswith("HTTP_") and isinstance(value, str):
                    headers[key[5:].lower().replace("_", "-")] = value
            request = Request(
                method=str(environ.get("REQUEST_METHOD", "GET")).upper(),
                path=str(environ.get("PATH_INFO", "/") or "/"),
                query={},
                payload={},
                request_id=headers.get("x-request-id", "") or uuid.uuid4().hex,
                headers=headers,
            )
            response = self._render_error(exc, request)
            start_response(*response.wsgi())
            return [response.body]
        response = self.respond(request)
        status, headers = response.wsgi()
        start_response(status, headers)
        return [response.body]


def _replace_payload(request: Request, payload: dict[str, object]) -> Request:
    return Request(
        method=request.method,
        path=request.path,
        query=request.query,
        payload=payload,
        request_id=request.request_id,
        headers=request.headers,
    )


# Routing errors carry their own stable codes so they join the same pipeline.
class _RouteNotFound(NotFound):
    status = 404
    code = "ROUTE_NOT_FOUND"


class _MethodNotAllowed(ConsoleError):
    status = 405
    code = "METHOD_NOT_ALLOWED"


class _InternalError(ConsoleError):
    status = 500
    code = "INTERNAL_ERROR"


def create_application(store: Store) -> Server:
    """Build a ready to serve console for the supplied store."""

    return Server(store)
