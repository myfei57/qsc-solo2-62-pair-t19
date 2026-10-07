"""WSGI application that serves the control console."""

from __future__ import annotations

import json
from typing import Callable, Iterable

from coldstore.errors import (
    ConsoleError,
    FieldError,
    MethodNotAllowedError,
    RouteNotFoundError,
    UpstreamError,
)
from coldstore.store import Store

from . import handlers
from .errors import contract_descriptor, negotiate, negotiate_environ, render_error
from .http import Request, Response, Router, build_request
from .metrics import Metrics
from .routes import register_routes
from .runtime import Runtime

# Route and method misses are transport noise; everything else is a contract
# decision and must be traceable in the audit trail.
_UNAUDITED_CODES = ("ROUTE_NOT_FOUND", "METHOD_NOT_ALLOWED")


class Server:
    """Owns the runtime state and dispatches console requests."""

    def __init__(self, store: Store) -> None:
        self.runtime = Runtime(store)
        self.router = Router()
        self.metrics = Metrics()
        register_routes(self.router, handlers)
        # Register the served contract so every later audit entry can be
        # read against the exact routes, codes, and formats in force.
        self.runtime.auditor.record("contract", contract_descriptor())

    def respond(self, request: Request) -> Response:
        response = self._route(request)
        self.metrics.record(request.method, request.path, response.status)
        return response

    def _route(self, request: Request) -> Response:
        handler = self.router.match(request.method, request.path)
        if handler is None:
            if self.router.allows(request.path):
                return self._fail(
                    request,
                    MethodNotAllowedError(
                        f"method {request.method} not allowed for {request.path}"
                    ),
                )
            return self._fail(request, RouteNotFoundError(f"no route for {request.path}"))
        try:
            return handler(self, request)
        except ConsoleError as exc:
            return self._fail(request, exc)
        except ValueError as exc:
            # A domain raise that has not been classified yet still maps to
            # the field rejection semantics it always had.
            return self._fail(request, FieldError(str(exc)))
        except OSError as exc:
            # The persisted store is the downstream dependency of every
            # handler; its failures are transient for the caller.
            return self._fail(request, UpstreamError("persistent store failure", cause=exc))
        except Exception:  # noqa: BLE001 - last resort for a live console
            return self._fail(request, ConsoleError())

    def _fail(self, request: Request, error: ConsoleError) -> Response:
        self._audit_error(request, error)
        return render_error(error, negotiate(request))

    def _audit_error(self, request: Request, error: ConsoleError) -> None:
        spec = error.spec()
        if spec.code in _UNAUDITED_CODES:
            return
        kind = "validation" if spec.status < 500 else "error"
        detail = json.dumps(
            {
                "code": spec.code,
                "category": spec.category,
                "field": error.field,
                "method": request.method,
                "path": request.path,
                "status": spec.status,
            },
            sort_keys=True,
        )
        try:
            self.runtime.auditor.record(kind, detail)
        except OSError:
            # Auditing must never mask the original failure, least of all
            # when the store itself is the dependency that just failed.
            pass

    def dispatch(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """Call a route directly, used by the test suite and CLI probes."""

        request = Request(
            method=method.upper(),
            path=path,
            query={},
            payload=dict(payload or {}),
            headers={key.lower(): value for key, value in dict(headers or {}).items()},
        )
        return self.respond(request)

    def __call__(
        self, environ: dict[str, object], start_response: Callable[..., object]
    ) -> Iterable[bytes]:
        try:
            response = self.respond(build_request(environ))
        except ConsoleError as exc:
            response = render_error(exc, negotiate_environ(environ))
        status, headers = response.wsgi()
        start_response(status, headers)
        return [response.body]


def create_application(store: Store) -> Server:
    """Build a ready to serve console for the supplied store."""

    return Server(store)
