"""Registry of every console route contract.

This is the *one* place where request shapes and their possible error codes are
declared. The route table in :mod:`routes` binds handlers; this module binds
the contract for the same method+path. The gateway runs ``contract.validate``
before invoking a handler, the catalog publishes ``contract.as_dict`` and the
audit trail records validation results against the same registration.

GET routes that accept body fields (a legacy quirk used by old clients) are
registered too, so validation is identical whether arguments arrive in a POST
body or a GET body.
"""

from __future__ import annotations

from .contract import Contract, FieldSpec, s

_ID = s("id", required=True)
_ROOM = s("room")
_GROUP = s("group", default="cold", choices=("cold", "evap"))

# Error families reused across operations.
_NOT_FOUND = ("NOT_FOUND",)
_STATE = ("STATE_INVALID",)
_CONFLICT = ("CONFLICT",)


def _contracts() -> dict[tuple[str, str], Contract]:
    table: list[Contract] = [
        # --- console / history / metrics ----------------------------------
        Contract("POST", "/history", (s("kind", required=True), s("value"))),
        Contract("POST", "/ops/reset-store"),
        Contract("POST", "/metrics/reset"),
        # --- rooms ---------------------------------------------------------
        Contract(
            "POST", "/room/add",
            (s("id", required=True), s("zone", "int", minimum=0, maximum=100_000),
             s("temp", "float", default=0.0, minimum=-100, maximum=100)),
            _CONFLICT,
        ),
        Contract("POST", "/room/remove", (_ID,), _NOT_FOUND),
        Contract(
            "POST", "/room/assign", (s("id", required=True), s("compressor", required=True)), _NOT_FOUND
        ),
        Contract(
            "POST", "/room/sensor", (s("id", required=True), s("sensor", required=True)), _NOT_FOUND
        ),
        Contract(
            "POST", "/room/renumber",
            (s("id", required=True), s("zone", "int", minimum=0, maximum=100_000)),
            _NOT_FOUND + _CONFLICT,
        ),
        Contract(
            "POST", "/room/temp",
            (s("id", required=True), s("temp", "float", minimum=-100, maximum=100)),
            _NOT_FOUND,
        ),
        Contract("GET", "/room/temp", (s("id", required=True),)),
        Contract("POST", "/room/lookup", (_ID,)),
        Contract("GET", "/room/compressor", (s("compressor", required=True),)),
        Contract("POST", "/room/reset"),
        # --- temperature ---------------------------------------------------
        Contract(
            "POST", "/temp/read",
            (s("value", "float", minimum=-100, maximum=100),),
        ),
        Contract("POST", "/temp/window", (s("window", "float"),)),
        Contract("POST", "/temp/reset"),
        # --- compressors ---------------------------------------------------
        Contract(
            "POST", "/comp/add",
            (s("id", required=True), s("stages", "int", default=1, minimum=1, maximum=8)),
            _CONFLICT,
        ),
        Contract("POST", "/comp/remove", (_ID,), _NOT_FOUND),
        Contract("POST", "/comp/lookup", (_ID,)),
        Contract(
            "POST", "/comp/stage",
            (s("id", required=True), s("stage", "int", minimum=0, maximum=8)),
            _NOT_FOUND + _STATE,
        ),
        Contract("POST", "/comp/start", (_ID,), _NOT_FOUND),
        Contract("POST", "/comp/stop", (_ID,), _NOT_FOUND),
        Contract(
            "POST", "/comp/reserve",
            (s("id", required=True), s("room", required=True)),
            _NOT_FOUND + _CONFLICT,
        ),
        Contract(
            "POST", "/comp/release",
            (s("id", required=True), s("room", required=True)),
            _NOT_FOUND + _CONFLICT,
        ),
        # --- valves --------------------------------------------------------
        Contract("POST", "/valve/open", (_ID,), _NOT_FOUND),
        Contract("POST", "/valve/close", (_ID,), _NOT_FOUND),
        Contract(
            "POST", "/valve/position",
            (s("id", required=True),
             s("position", choices=("open", "closed"), label="valve position")),
            _NOT_FOUND,
        ),
        Contract("POST", "/valve/start", (_ID,), _NOT_FOUND + _STATE),
        Contract("POST", "/valve/stop", (_ID,), _NOT_FOUND),
        # --- fans ----------------------------------------------------------
        Contract(
            "POST", "/fan/add",
            (s("id", required=True), s("role", default="standby", choices=("primary", "standby")),
             _GROUP),
            _CONFLICT,
        ),
        Contract("GET", "/fan", (_GROUP,)),
        Contract("POST", "/fan/lookup", (_ID, _GROUP)),
        Contract("POST", "/fan/start", (_ID, _GROUP), _NOT_FOUND + _STATE),
        Contract("POST", "/fan/stop", (_ID, _GROUP), _NOT_FOUND),
        Contract("POST", "/fan/switch", (_ID, _GROUP), _NOT_FOUND),
        Contract("POST", "/fan/fail", (_ID, _GROUP), _NOT_FOUND),
        Contract("POST", "/fan/repair", (_ID, _GROUP), _NOT_FOUND),
        Contract("GET", "/fan/faults", (_GROUP,)),
        # --- evaporative condenser -----------------------------------------
        Contract(
            "POST", "/evap/baseline",
            (s("baseline", "float", minimum=-100, maximum=100),),
        ),
        Contract(
            "POST", "/evap/temp",
            (s("temperature", "float", minimum=-100, maximum=100),),
        ),
        Contract("POST", "/evap/switch", (_ID,), _NOT_FOUND),
        # --- defrost -------------------------------------------------------
        Contract("POST", "/defrost/start", (_ID,), _NOT_FOUND),
        Contract("POST", "/defrost/finish", (_ID,), _NOT_FOUND),
        Contract("POST", "/defrost/enqueue", (_ID,)),
        Contract("POST", "/defrost/replay"),
        Contract("POST", "/defrost/recover"),
        Contract("POST", "/defrost/select", (s("zone", "int"),), _NOT_FOUND),
        # --- ammonia -------------------------------------------------------
        Contract("POST", "/ammonia/read", (s("ppm", "float", minimum=0, maximum=1_000_000),)),
        Contract(
            "POST", "/ammonia/threshold",
            (s("threshold", "float", minimum=0.000001, maximum=1_000_000),),
        ),
        Contract("POST", "/ammonia/calibrate", (s("factor", "float"),)),
        Contract("POST", "/ammonia/reset"),
        # --- interlock -----------------------------------------------------
        Contract("POST", "/interlock/trip", (s("reason", default="manual"),)),
        Contract("POST", "/interlock/reset"),
        Contract("POST", "/interlock/clear"),
        # --- load ----------------------------------------------------------
        Contract(
            "POST", "/load/baseline",
            (s("baseline", "float", minimum=-100, maximum=100),),
        ),
        Contract("POST", "/load/coefficient", (s("coefficient", "float"),)),
        Contract("POST", "/load/estimate", (s("temp", "float"),)),
        # --- quota ---------------------------------------------------------
        Contract(
            "POST", "/quota/add", (s("amount", "float", minimum=0, maximum=1_000_000),)
        ),
        Contract("POST", "/quota/remaining", (s("limit", "float"),)),
        Contract(
            "POST", "/quota/check",
            (s("resource", required=True), s("used", "float"), s("limit", "float")),
        ),
        Contract("POST", "/quota/reset"),
        # --- audit ---------------------------------------------------------
        Contract("POST", "/audit/filter", (s("kind", required=True),)),
        # --- schedule ------------------------------------------------------
        Contract(
            "POST", "/schedule/threshold",
            (s("threshold", "float", minimum=-100, maximum=100),),
        ),
        Contract("POST", "/schedule/plan"),
        # --- cycle / simulation -------------------------------------------
        Contract(
            "POST", "/cycle",
            (s("room_id"), s("comp_id"), s("temp", "float", minimum=-100, maximum=100),
             s("ppm", "float", minimum=0, maximum=1_000_000),
             s("amount", "float", minimum=0, maximum=1_000_000), s("defrost", "int", default=0)),
        ),
        Contract("POST", "/simulate", (s("ticks", "int", default=5, minimum=1, maximum=100),)),
    ]
    return {(c.method, c.path): c for c in table}


CONTRACTS: dict[tuple[str, str], Contract] = _contracts()


def contract_for(method: str, path: str) -> Contract | None:
    return CONTRACTS.get((method.upper(), path))


def all_contracts() -> list[Contract]:
    return [CONTRACTS[key] for key in sorted(CONTRACTS)]
