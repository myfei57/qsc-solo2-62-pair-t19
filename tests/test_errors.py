"""Acceptance tests for the unified validation and error semantics."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock

from coldstore.console.seed import seed_defaults
from coldstore.console.server import Server
from coldstore.errors import ERROR_REGISTRY
from coldstore.store import Store

V2 = {"X-Error-Format": "v2"}


class ErrorSemanticsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        response = self.server.dispatch(method, path, payload, headers=headers)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response, parsed

    def test_v1_envelope_is_the_default_and_byte_compatible(self) -> None:
        response, body = self.call("POST", "/temp/read", {"value": -500})
        self.assertEqual(response.status, 400)
        # Legacy shape: a flat {"error": message} and nothing else.
        self.assertEqual(body, {"error": "temp is below the supported range"})
        header_map = dict(response.extra_headers)
        self.assertEqual(header_map["Deprecation"], "true")
        self.assertIn("v2", header_map["Sunset"])

    def test_v2_envelope_is_negotiated_by_header(self) -> None:
        response, body = self.call("POST", "/temp/read", {"value": -500}, headers=V2)
        self.assertEqual(response.status, 400)
        self.assertEqual(
            body["error"],
            {
                "code": "FIELD_INVALID",
                "message": "temp is below the supported range",
                "category": "field",
                "retryable": False,
                "field": "temp",
            },
        )

    def test_type_coercion_names_the_field(self) -> None:
        response, body = self.call("POST", "/temp/read", {"value": "abc"}, headers=V2)
        self.assertEqual(response.status, 400)
        self.assertEqual(body["error"]["code"], "FIELD_INVALID")
        self.assertEqual(body["error"]["field"], "value")
        self.assertIn("must be a number", body["error"]["message"])

    def test_missing_entity_is_a_404_not_a_400(self) -> None:
        response, body = self.call("POST", "/room/remove", {"id": "ghost"}, headers=V2)
        self.assertEqual(response.status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")
        self.assertEqual(body["error"]["category"], "not_found")
        self.assertFalse(body["error"]["retryable"])

    def test_state_rejection_is_a_409(self) -> None:
        self.call("POST", "/fan/add", {"id": "f1", "role": "primary"})
        self.call("POST", "/fan/fail", {"id": "f1"})
        response, body = self.call("POST", "/fan/start", {"id": "f1"}, headers=V2)
        self.assertEqual(response.status, 409)
        self.assertEqual(body["error"]["code"], "STATE_INVALID")
        self.assertEqual(body["error"]["category"], "state")

    def test_conflicting_reservation_is_a_409(self) -> None:
        self.call("POST", "/comp/add", {"id": "c1", "stages": 4})
        self.call("POST", "/comp/reserve", {"id": "c1", "room": "r1"})
        response, body = self.call("POST", "/comp/reserve", {"id": "c1", "room": "r2"}, headers=V2)
        self.assertEqual(response.status, 409)
        self.assertEqual(body["error"]["code"], "CONFLICT")
        self.assertEqual(body["error"]["category"], "conflict")

    def test_downstream_failure_is_retryable(self) -> None:
        with mock.patch.object(self.store, "put", side_effect=OSError("disk full")):
            response, body = self.call("POST", "/temp/window", {"window": 1.5}, headers=V2)
        self.assertEqual(response.status, 503)
        self.assertEqual(body["error"]["code"], "UPSTREAM_FAILURE")
        self.assertEqual(body["error"]["category"], "upstream")
        self.assertTrue(body["error"]["retryable"])

    def test_downstream_failure_does_not_leak_internals_to_v1(self) -> None:
        with mock.patch.object(self.store, "put", side_effect=OSError("disk full")):
            response, body = self.call("POST", "/temp/window", {"window": 1.5})
        self.assertEqual(response.status, 503)
        self.assertEqual(body, {"error": "persistent store failure"})
        self.assertNotIn("disk", body["error"])

    def test_internal_error_is_generic_in_both_formats(self) -> None:
        with mock.patch.object(
            self.server.runtime.temp, "state", side_effect=RuntimeError("secret internals")
        ):
            _, body_v2 = self.call("GET", "/temp", headers=V2)
            _, body_v1 = self.call("GET", "/temp")
        self.assertEqual(body_v2["error"]["code"], "INTERNAL")
        self.assertEqual(body_v2["error"]["message"], "an unexpected internal error occurred")
        self.assertEqual(body_v1, {"error": "an unexpected internal error occurred"})
        self.assertNotIn("secret", json.dumps(body_v2))

    def test_contract_is_registered_at_startup(self) -> None:
        _, summary = self.call("GET", "/audit/summary")
        self.assertEqual(summary["by_kind"], {"contract": 1})
        _, listing = self.call("GET", "/audit")
        contract = json.loads(listing["entries"][0]["detail"])
        self.assertEqual(contract["errors"], sorted(ERROR_REGISTRY))
        self.assertEqual(contract["formats"], ["v1", "v2"])
        self.assertGreater(contract["routes"], 0)

    def test_validation_rejections_are_audited_with_the_code(self) -> None:
        self.call("POST", "/temp/read", {"value": -500})
        self.call("POST", "/room/remove", {"id": "ghost"})
        _, listing = self.call("GET", "/audit")
        validations = [
            json.loads(entry["detail"])
            for entry in listing["entries"]
            if entry["kind"] == "validation"
        ]
        self.assertEqual(
            [(item["code"], item["field"], item["path"]) for item in validations],
            [
                ("FIELD_INVALID", "temp", "/temp/read"),
                ("NOT_FOUND", "", "/room/remove"),
            ],
        )

    def test_route_misses_stay_out_of_the_audit_trail(self) -> None:
        self.call("GET", "/missing")
        self.call("DELETE", "/health")
        _, summary = self.call("GET", "/audit/summary")
        self.assertEqual(summary["by_kind"], {"contract": 1})

    def test_catalog_and_report_export_share_one_registry(self) -> None:
        _, catalog = self.call("GET", "/catalog")
        api_codes = [entry["code"] for entry in catalog["errors"]]

        response, csv_body = self.call("GET", "/export/errors")
        self.assertEqual(response.status, 200)
        lines = csv_body.strip().split("\n")
        self.assertEqual(lines[0], "code,status,category,retryable,summary")
        report_codes = [line.split(",")[0] for line in lines[1:]]

        self.assertEqual(api_codes, report_codes)
        self.assertEqual(set(api_codes), set(ERROR_REGISTRY))
        self.assertIn("UPSTREAM_FAILURE", api_codes)


if __name__ == "__main__":
    unittest.main()
