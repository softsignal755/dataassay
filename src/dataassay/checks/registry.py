"""The catalog: every check, versioned independently of the engine.

The version is stamped into each audit, so "we found nothing" is a statement
about a specific catalog rather than a timeless claim, and re-running an old
audit against a newer catalog is a meaningful act.
"""

from __future__ import annotations

from dataassay.checks.base import Check
from dataassay.checks.distribution import LevelShift, SaturationAtBound
from dataassay.checks.precision import NegligibleResidue
from dataassay.checks.structural import (
    ConstantNumericColumn,
    DuplicateGrain,
    DuplicateRows,
    Mojibake,
    SchemaDrift,
)
from dataassay.checks.temporal import CadenceGap, FlatlineTail, FutureDates

CATALOG_VERSION = "2026.08.4"

CATALOG: list[Check] = [
    ConstantNumericColumn(),
    SchemaDrift(),
    Mojibake(),
    DuplicateRows(),
    DuplicateGrain(),
    FutureDates(),
    CadenceGap(),
    FlatlineTail(),
    SaturationAtBound(),
    NegligibleResidue(),
    LevelShift(),
]


def catalog_dict() -> dict:
    return {
        "catalog_version": CATALOG_VERSION,
        "checks": [c.spec.to_dict() for c in CATALOG],
    }
