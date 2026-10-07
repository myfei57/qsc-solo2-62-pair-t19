"""Tests for the unified validation and error semantics (v2 envelope).

Covers the decisions that replaced the old "everything is 400" behaviour:

* field / state / conflict / not-found / retryable are reported separately
* the canonical envelope carries a stable code, request id and violations
* the legacy ``{"error": ...}`` body stays available during the compatibility
  window (per-request opt in, or the server default)
* validation runs centrally at the gateway, off registered contracts
* contracts, validation results and error codes are audited and the CSV
  export reads the same auditor the console endpoints use
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest

from coldstore.console.seed import seed_defaults
from coldstore.console.server import Server
from coldstore.errors import DownstreamUnavailable
from coldstore.store import Store


class ErrorSemanticsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        # Post-sunset default: canonical envelope unless legacy is requested.
        self.server = Server(self.store, legacy_errors_by_default=False)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload=None, headers=None, query=""):
        body = json.dumps(payload).encode() if payload is not None else b""
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        for key, value in (headers or {}).items():
            environ["HTTP_" + key.upper().replace("-", "_")] = value
        captured: dict[str, object] = {}

        def start_response(status, response_headers):
            captured["status"] = status
            captured["headers"] = dict(response_headers)

        raw = b"".join(self.server(environ, start_response))
        parsed = json.loads(raw)
        status_line = str(captured["status"])
        status = int(status_line.split(" ", 1)[0])
        return status, captured["headers"], parsed

    # ------------------------------------------------------------------
    # classification
    # ------------------------------------------------------------------
    def test_field_state_conflict_not_found_are_separate(self) -> None:
        # field error: malformed input
        status, _, body = self.call("POST", "/room/add", {"id": "r1", "zone": -1})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "FIELD_INVALID")
        self.assertEqual(body["violations"][0]["field"], "zone")

        # not found
        status, _, body = self.call("POST", "/room/remove", {"id": "ghost"})
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "NOT_FOUND")

        # conflict: duplicate id
        self.call("POST", "/room/add", {"id": "r1", "zone": 1})
        status, _, body = self.call("POST", "/room/add", {"id": "r1", "zone": 2})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "CONFLICT")

        # state invalid: stage beyond the machine's own stage count
        self.call("POST", "/comp/add", {"id": "c1", "stages": 4})
        status, _, body = self.call("POST", "/comp/stage", {"id": "c1", "stage": 5})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "STATE_INVALID")

        # conflict distinct from state: reservation held by another room
        self.call("POST", "/comp/reserve", {"id": "c1", "room": "r1"})
        status, _, body = self.call("POST", "/comp/reserve", {"id": "c1", "room": "r2"})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "CONFLICT")

    def test_multiple_violations_are_aggregated(self) -> None:
        status, _, body = self.call(
            "POST", "/room/add", {"id": "r1", "zone": "bad", "temp": 999}
        )
        self.assertEqual(status, 400)
        fields = {v["field"]: v["code"] for v in body["violations"]}
        self.assertEqual(fields, {"zone": "TYPE", "temp": "RANGE"})

    def test_envelope_carries_request_id_and_retry_flag(self) -> None:
        status, headers, body = self.call(
            "POST", "/room/remove", {"id": "x"}, headers={"X-Request-Id": "abc123"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["request_id"], "abc123")
        self.assertFalse(body["retryable"])
        self.assertEqual(headers["X-Request-Id"], "abc123")

    def test_enum_and_required_violations(self) -> None:
        status, _, body = self.call("POST", "/fan/add", {"id": "f1", "role": "sideways"})
        self.assertEqual(body["violations"][0]["code"], "ENUM")

        status, _, body = self.call("POST", "/history", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["violations"][0]["code"], "REQUIRED")

    # ------------------------------------------------------------------
    # retryable downstream chain
    # ------------------------------------------------------------------
    def test_downstream_failure_is_retryable_503(self) -> None:
        def boom(bank, comp_id):
            raise DownstreamUnavailable("actuator bus timeout", retry_after=5)

        self.server.runtime.supply.start_with_supply = boom
        status, headers, body = self.call("POST", "/valve/start", {"id": "c1"})
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "DOWNSTREAM_UNAVAILABLE")
        self.assertTrue(body["retryable"])
        self.assertEqual(body["retry_after"], 5)
        self.assertEqual(headers["Retry-After"], "5")

    # ------------------------------------------------------------------
    # legacy compatibility
    # ------------------------------------------------------------------
    def test_legacy_envelope_opt_in_on_a_v2_default_server(self) -> None:
        status, _, body = self.call(
            "POST", "/room/remove", {"id": "ghost"}, headers={"X-Error-Format": "legacy"}
        )
        # Legacy clients still see the old status collapse and old body shape.
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "cold room ghost not found"})

    def test_legacy_envelope_via_query_parameter(self) -> None:
        status, _, body = self.call(
            "POST", "/room/remove", {"id": "ghost"}, query="error_format=legacy"
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_legacy_default_server_serves_legacy_until_sunset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/legacy.json")
            seed_defaults(store)
            server = Server(store, legacy_errors_by_default=True)
            raw = b'{"id": "ghost"}'
            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/room/remove",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(raw)),
                "wsgi.input": io.BytesIO(raw),
            }
            captured: dict[str, object] = {}
            data = b"".join(
                server(environ, lambda s, h: captured.update(status=s, headers=h))
            )
            self.assertTrue(str(captured["status"]).startswith("400"))
            self.assertIn("error", json.loads(data))
            store.close()

    # ------------------------------------------------------------------
    # contract governance: catalog + audit + shared export
    # ------------------------------------------------------------------
    def test_contracts_are_registered_and_published(self) -> None:
        status, _, body = self.call("GET", "/catalog")
        self.assertEqual(status, 200)
        contract = body["contracts"]["POST /comp/reserve"]
        self.assertIn("CONFLICT", contract["errors"])
        self.assertTrue(any(f["name"] == "id" for f in contract["fields"]))

    def test_validation_and_error_codes_are_audited(self) -> None:
        self.call("POST", "/room/remove", {"id": "ghost"})  # NOT_FOUND
        self.call("POST", "/room/add", {"id": "r1", "zone": -1})  # FIELD_INVALID
        status, body_status, _ = (None, None, None)
        _, _, summary = self.call_direct("GET", "/audit/summary")
        by_kind = summary["by_kind"]
        self.assertIn("contract", by_kind)
        self.assertIn("validation", by_kind)
        self.assertIn("error", by_kind)

        _, _, errors = self.call_direct("POST", "/audit/filter", {"kind": "error"})
        codes = {entry["result"]["code"] for entry in errors["entries"]}
        self.assertIn("NOT_FOUND", codes)
        self.assertIn("FIELD_INVALID", codes)
        for entry in errors["entries"]:
            self.assertTrue(entry["result"]["request_id"])

    def call_direct(self, method, path, payload=None):
        response = self.server.dispatch(method, path, payload)
        return response.status, {}, json.loads(response.body)

    def test_console_and_csv_export_share_one_audit_trail(self) -> None:
        self.call("POST", "/room/remove", {"id": "ghost"})
        _, _, console = self.call_direct("POST", "/audit/filter", {"kind": "error"})
        console_rows = {
            (e["time"], e["result"]["code"]) for e in console["entries"]
        }
        response = self.server.dispatch("GET", "/export/audit")
        csv_text = response.body.decode("utf-8")
        header = csv_text.splitlines()[0]
        self.assertEqual(header, "time,kind,detail,id,route,code,status")
        # Every console error row must appear in the CSV, with the same code.
        for _, code in console_rows:
            self.assertIn(code, csv_text)

    def test_contracts_are_registered_once_across_restart(self) -> None:
        _, _, first = self.call_direct("GET", "/audit/summary")
        # Rebuild the server over the same persisted store.
        Server(self.store, legacy_errors_by_default=False)
        _, _, second = self.call_direct("GET", "/audit/summary")
        self.assertEqual(
            first["by_kind"]["contract"], second["by_kind"]["contract"]
        )


if __name__ == "__main__":
    unittest.main()
