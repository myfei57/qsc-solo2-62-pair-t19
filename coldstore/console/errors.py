"""Error envelope rendering, format negotiation, and the contract descriptor."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from coldstore.errors import ERROR_REGISTRY, ConsoleError

from .http import Response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .http import Request

FORMAT_V1 = "v1"
FORMAT_V2 = "v2"
SUPPORTED_FORMATS = (FORMAT_V1, FORMAT_V2)
FORMAT_HEADER = "x-error-format"

# Legacy clients keep the v1 envelope for the whole compatibility window;
# the header advertises the sunset so monitors can scrape migration progress.
SUNSET_NOTE = "v1 error envelope is deprecated; send X-Error-Format: v2"


def negotiate(request: "Request") -> str:
    """Pick the error envelope the client asked for, defaulting to legacy."""

    wanted = request.headers.get(FORMAT_HEADER, "").strip().lower()
    return FORMAT_V2 if wanted == FORMAT_V2 else FORMAT_V1


def negotiate_environ(environ: dict[str, object]) -> str:
    """Pick the envelope straight from a WSGI environ, for parse failures."""

    wanted = str(environ.get("HTTP_X_ERROR_FORMAT", "")).strip().lower()
    return FORMAT_V2 if wanted == FORMAT_V2 else FORMAT_V1


def render_error(error: ConsoleError, fmt: str) -> Response:
    """Render a classified error in the negotiated envelope."""

    spec = error.spec()
    if fmt == FORMAT_V2:
        detail: dict[str, object] = {
            "code": spec.code,
            "message": error.detail,
            "category": spec.category,
            "retryable": spec.retryable,
        }
        if error.field:
            detail["field"] = error.field
        return json_response({"error": detail}, status=spec.status)
    # v1 keeps the legacy shape and message text byte compatible, and
    # advertises the sunset so the compatibility window is observable.
    return json_response(
        {"error": error.detail},
        status=spec.status,
        extra_headers=(("Deprecation", "true"), ("Sunset", SUNSET_NOTE)),
    )


def contract_descriptor() -> str:
    """The registered contract: route count, error codes, envelope formats."""

    from .routes import route_table  # deferred to avoid an import cycle

    return json.dumps(
        {
            "routes": len(route_table()),
            "errors": sorted(ERROR_REGISTRY),
            "formats": list(SUPPORTED_FORMATS),
        },
        sort_keys=True,
    )
