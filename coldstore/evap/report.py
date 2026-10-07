"""Evaporative condenser projections and validation rules."""

from __future__ import annotations

from coldstore.errors import FieldMalformed

from dataclasses import dataclass


@dataclass(frozen=True)
class CondenserState:
    """Reading, baseline and the duty fan serving the condenser."""

    temperature: float
    baseline: float
    above_baseline: bool
    duty: str

    def as_dict(self) -> dict[str, object]:
        return {
            "temperature": self.temperature,
            "baseline": self.baseline,
            "above_baseline": self.above_baseline,
            "duty": self.duty,
        }


def validate_baseline(value: float) -> None:
    """Reject baselines outside the supported range."""

    if value < -100:
        raise FieldMalformed("baseline is below the supported range")
    if value > 100:
        raise FieldMalformed("baseline exceeds the supported range")
