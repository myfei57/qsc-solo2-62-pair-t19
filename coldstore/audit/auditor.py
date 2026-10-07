"""Append only audit entries for every control decision.

The trail records two shapes, in one stream, from one store:

* legacy entries ``{id,time,kind,detail}`` written by older builds, and
* structured entries that additionally carry ``route``, ``contract`` and
  ``result`` (validation outcomes and classified error codes).

Console endpoints (``/audit*``) and report delivery (``/export/audit``) both
read :class:`Auditor` entries, so registration and reporting can never drift.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

from .report import AuditState

AUDIT_KEY = "audit:entries"

# Structured event kinds.
KIND_CONTRACT = "contract"
KIND_VALIDATION = "validation"
KIND_ERROR = "error"
KIND_REQUEST = "request"


@dataclass(frozen=True)
class Entry:
    """One recorded control decision or governance event."""

    id: str
    time: str
    kind: str
    detail: str
    route: str = ""
    contract: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"id": self.id, "time": self.time, "kind": self.kind, "detail": self.detail}
        if self.route:
            body["route"] = self.route
        if self.contract:
            body["contract"] = self.contract
        if self.result:
            body["result"] = self.result
        return body


class Auditor:
    """Records and queries runtime entries."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record(self, kind: str, detail: str) -> Entry:
        return self._write(Entry(id=self._new_id(), time=self._now(), kind=kind, detail=detail))

    def record_contract(self, route: str, contract: dict[str, Any]) -> Entry:
        """Register the contract (fields + error codes) for a served route."""

        return self._write(
            Entry(
                id=self._new_id(),
                time=self._now(),
                kind=KIND_CONTRACT,
                detail=route,
                route=route,
                contract=contract,
                result={"registered": True},
            )
        )

    def record_validation(
        self,
        route: str,
        ok: bool,
        *,
        violations: list[dict[str, Any]] | None = None,
        request_id: str = "",
    ) -> Entry:
        """Register the outcome of gateway field validation for a request."""

        result: dict[str, Any] = {"ok": ok, "request_id": request_id}
        if violations:
            result["violations"] = violations
        return self._write(
            Entry(
                id=self._new_id(),
                time=self._now(),
                kind=KIND_VALIDATION,
                detail=route,
                route=route,
                result=result,
            )
        )

    def record_error(
        self,
        route: str,
        status: int,
        code: str,
        *,
        message: str = "",
        retryable: bool = False,
        request_id: str = "",
    ) -> Entry:
        """Register a classified error code returned to a client."""

        return self._write(
            Entry(
                id=self._new_id(),
                time=self._now(),
                kind=KIND_ERROR,
                detail=code,
                route=route,
                result={
                    "status": status,
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "request_id": request_id,
                },
            )
        )

    def _write(self, entry: Entry) -> Entry:
        append_command(self._store, AUDIT_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def entries(self) -> list[Entry]:
        entries: list[Entry] = []
        for item in load_commands(self._store, AUDIT_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            contract = payload.get("contract")
            result = payload.get("result")
            entries.append(
                Entry(
                    id=str(payload.get("id", "")),
                    time=str(payload.get("time", "")),
                    kind=str(payload.get("kind", "")),
                    detail=str(payload.get("detail", "")),
                    route=str(payload.get("route", "")),
                    contract=contract if isinstance(contract, dict) else {},
                    result=result if isinstance(result, dict) else {},
                )
            )
        return entries

    def last(self) -> Entry | None:
        entries = self.entries()
        if not entries:
            return None
        return entries[-1]

    def count(self) -> int:
        return len(self.entries())

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts

    def filter(self, kind: str) -> list[Entry]:
        return [entry for entry in self.entries() if entry.kind == kind]

    def kinds(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries():
            if entry.kind not in seen:
                seen.append(entry.kind)
        return seen

    def totals(self) -> dict[str, float]:
        """Sum the numeric amount recorded for each entry kind."""

        totals: dict[str, float] = {}
        for entry in self.entries():
            try:
                amount = float(entry.detail)
            except ValueError:
                continue
            totals[entry.kind] = totals.get(entry.kind, 0.0) + amount
        return totals

    def state(self) -> AuditState:
        entries = self.entries()
        return AuditState(entries=[entry.as_dict() for entry in entries], count=len(entries))

    def describe(self) -> str:
        return f"audit entries={len(self.entries())}"
