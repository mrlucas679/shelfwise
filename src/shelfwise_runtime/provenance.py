"""Small provenance vocabulary shared by live serving and training paths."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class DataDomain(StrEnum):
    """Identify the trust boundary a record is allowed to cross."""

    OPERATIONAL_TWIN = "operational_twin"
    WORLD_SIMULATION = "world_simulation"
    TRAINING_FIXTURE = "training_fixture"
    TWIN_SCENARIO = "twin_scenario"


class DataDomainBoundaryError(ValueError):
    """Report a record that attempted to cross into an incompatible data domain."""

    def __init__(
        self,
        *,
        boundary: str,
        actual: DataDomain | str,
        expected: DataDomain,
    ) -> None:
        self.boundary = boundary
        self.actual = DataDomain(str(actual))
        self.expected = expected
        super().__init__(
            f"{boundary} requires data_domain={expected.value!r}; "
            f"received {self.actual.value!r}"
        )


TRAINING_DOMAINS = frozenset(
    {DataDomain.WORLD_SIMULATION.value, DataDomain.TRAINING_FIXTURE.value}
)


def normalize_domain(value: Any, *, default: DataDomain) -> str:
    """Return a validated domain string for JSON records at a trust boundary."""
    candidate = str(value or default.value).strip().lower()
    allowed = {item.value for item in DataDomain}
    if candidate not in allowed:
        raise ValueError(f"unsupported data_domain: {candidate!r}")
    return candidate


def require_training_domain(value: Any) -> str:
    """Reject operational or scenario state from entering a training dataset."""
    domain = normalize_domain(value, default=DataDomain.TRAINING_FIXTURE)
    if domain not in TRAINING_DOMAINS:
        raise ValueError(
            f"training dataset cannot consume data_domain={domain!r}; "
            "export a reviewed training fixture instead"
        )
    return domain
