"""Declarative request contracts shared by gateway validation and the catalog.

A contract is the single registered source of truth for one route: which
fields it accepts, their type/shape constraints and which classified error
codes the operation can produce. The gateway validator runs the field rules
before a handler is called; the same registration feeds the catalog and the
audit trail, so the console, reports and exports all read one contract.

Field rules intentionally cover *structural* validation only (required,
type, range, enum). State and conflict rules stay in the domain layer because
only it holds the locks that make those checks truthful.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import FieldInvalid, FieldViolation

# Error codes any operation may produce in addition to its own list.
_COMMON_CODES = ("FIELD_INVALID", "NOT_FOUND", "INTERNAL_ERROR")


def _below_message(label: str, minimum: float) -> str:
    # Match the wording the domain validators used before validation was
    # centralised, so legacy clients keep seeing the same descriptions.
    if minimum == 0:
        return f"field {label} must be non-negative"
    if minimum == 1:
        return f"field {label} must be positive"
    return f"field {label} is below the supported range"


def _above_message(label: str) -> str:
    return f"field {label} exceeds the supported range"


@dataclass(frozen=True)
class FieldSpec:
    """Validation rule for one request field."""

    name: str
    type: str = "str"  # str | int | float | bool
    required: bool = False
    default: object = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    # Human facing name when it differs from the wire field (e.g. "position"
    # is the "valve position" old messages referred to).
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", self.name)
        if not self.required and self.default is None:
            object.__setattr__(self, "default", _type_default(self.type))

    def validate(self, payload: dict[str, object]) -> tuple[object, FieldViolation | None]:
        present = self.name in payload and payload.get(self.name) is not None
        if not present:
            if self.required:
                return None, FieldViolation(
                    self.name, "REQUIRED", f"field {self.name} is required"
                )
            return self.default, None
        return _coerce(self, payload[self.name])


def _type_default(type_name: str) -> object:
    if type_name == "int":
        return 0
    if type_name in ("float", "bool"):
        return 0.0 if type_name == "float" else False
    return ""


def _coerce(spec: FieldSpec, raw: object) -> tuple[object, FieldViolation | None]:
    name = spec.name
    label = spec.label
    if spec.type == "str":
        if not isinstance(raw, str):
            return None, FieldViolation(name, "TYPE", f"field {label} must be a string")
        value: object = raw
    elif spec.type == "bool":
        if not isinstance(raw, bool):
            return None, FieldViolation(name, "TYPE", f"field {label} must be a boolean")
        value = raw
    else:  # numeric
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            kind = "a number" if spec.type == "float" else "an integer"
            return None, FieldViolation(name, "TYPE", f"field {label} must be {kind}")
        try:
            number = float(raw)
        except ValueError:
            kind = "a number" if spec.type == "float" else "an integer"
            return None, FieldViolation(name, "TYPE", f"field {label} must be {kind}")
        if spec.type == "int":
            if not number.is_integer():
                return None, FieldViolation(name, "TYPE", f"field {label} must be an integer")
            value = int(number)
        else:
            value = number
        if spec.minimum is not None and number < spec.minimum:
            return None, FieldViolation(name, "RANGE", _below_message(label, spec.minimum))
        if spec.maximum is not None and number > spec.maximum:
            return None, FieldViolation(name, "RANGE", _above_message(label))
    if spec.choices is not None and value not in spec.choices:
        allowed = " or ".join(spec.choices)
        return None, FieldViolation(name, "ENUM", f"field {label} must be {allowed}")
    return value, None


@dataclass(frozen=True)
class Contract:
    """Registered contract for one route."""

    method: str
    path: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)
    # Classified business error codes this route may raise beyond the common set.
    errors: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "path": self.path,
            "fields": [
                {
                    "name": f.name,
                    "type": f.type,
                    "required": f.required,
                    "default": f.default,
                    "minimum": f.minimum,
                    "maximum": f.maximum,
                    "choices": list(f.choices) if f.choices else None,
                }
                for f in self.fields
            ],
            "errors": self.error_codes(),
        }

    def error_codes(self) -> list[str]:
        codes = list(_COMMON_CODES)
        for code in self.errors:
            if code not in codes:
                codes.append(code)
        return codes

    def validate(self, payload: dict[str, object]) -> dict[str, object]:
        """Return a normalised payload or raise one aggregated FieldInvalid."""

        result: dict[str, object] = {}
        violations: list[FieldViolation] = []
        for spec in self.fields:
            value, violation = spec.validate(payload)
            if violation is not None:
                violations.append(violation)
            else:
                result[spec.name] = value
        if violations:
            raise FieldInvalid(
                violations[0].message,
                violations=violations,
            )
        return result


def s(name: str, type: str = "str", **kwargs: object) -> FieldSpec:
    """Shorthand constructor used by the registration table."""

    return FieldSpec(name=name, type=type, **kwargs)  # type: ignore[arg-type]
