"""Orchestration: profile, infer structure, gate the checks, rank what survives.

Coverage is a first-class output, not a footnote. A reader cannot interpret an
empty findings list without knowing how many checks ran, which were withheld,
and which are waiting on an answer -- zero findings at 30% coverage and zero at
95% are entirely different objects, and one number cannot tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from dataassay import structure as structure_mod
from dataassay.checks.base import Confidence, Finding
from dataassay.checks.registry import CATALOG, CATALOG_VERSION
from dataassay.profile import Profile, build
from dataassay.provenance import reader_for, source_expr
from dataassay.structure import Structure


@dataclass
class Coverage:
    ran: list[str] = field(default_factory=list)
    withheld: list[tuple[str, str]] = field(default_factory=list)
    blocked: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.ran) + len(self.withheld) + len(self.blocked)

    def to_dict(self) -> dict:
        return {
            "checks_total": self.total,
            "ran": self.ran,
            "withheld": [{"check": c, "reason": r} for c, r in self.withheld],
            "blocked": [{"check": c, "question": qn} for c, qn in self.blocked],
        }


@dataclass
class Audit:
    profile: Profile
    structure: Structure
    findings: list[Finding]
    coverage: Coverage
    catalog_version: str = CATALOG_VERSION

    def to_dict(self) -> dict:
        d = self.profile.to_dict()
        d |= {
            "catalog_version": self.catalog_version,
            "structure": self.structure.to_dict(),
            "coverage": self.coverage.to_dict(),
            "findings": [f.to_dict() for f in self.findings],
        }
        return d


def _corroborate(findings: list[Finding]) -> list[Finding]:
    """Promote findings that two independent checks agree on.

    Agreement is the strongest confidence signal available, and the only one
    that does not depend on trusting a single detector's threshold. A column
    flagged by both a saturation check and a level-shift check is real in a way
    that either alone is not.
    """
    by_column: dict[str, set[str]] = {}
    for f in findings:
        if f.column:
            by_column.setdefault(f.column, set()).add(f.check_id)

    out = []
    for f in findings:
        others = by_column.get(f.column or "", set()) - {f.check_id}
        if others and f.confidence.level != "high":
            f = Finding(
                check_id=f.check_id,
                column=f.column,
                disposition=f.disposition,
                summary=f.summary,
                evidence=f.evidence,
                predicate=f.predicate,
                confidence=f.confidence.corroborated_by(", ".join(sorted(others))),
                raw_values=f.raw_values,
            )
        out.append(f)
    return out


def run(path: Path, byte_cap: int | None = None) -> Audit:
    from dataassay.checks.base import CheckContext
    from dataassay.rawscan import BYTE_CAP

    reader = reader_for(path)
    con = duckdb.connect(":memory:")
    try:
        profile = build(path, byte_cap=byte_cap or BYTE_CAP, con=con)
        source = source_expr(reader, profile.provenance.lenient)
        params = [str(path)]
        struct = structure_mod.infer(
            profile.columns, con, source, params, profile.provenance.row_count
        )
        ctx = CheckContext(
            profile=profile, structure=struct, con=con, source=source, params=params
        )

        coverage = Coverage()
        findings: list[Finding] = []
        for check in CATALOG:
            verdict = check.applies(ctx)
            if not verdict.applicable:
                if verdict.blocked:
                    coverage.blocked.append((check.spec.id, verdict.reason))
                else:
                    coverage.withheld.append((check.spec.id, verdict.reason))
                continue
            coverage.ran.append(check.spec.id)
            findings.extend(check.run(ctx))
    finally:
        con.close()

    findings = _corroborate(findings)
    findings.sort(key=lambda f: f.sort_key)
    return Audit(
        profile=profile, structure=struct, findings=findings, coverage=coverage
    )


__all__ = ["Audit", "Coverage", "Confidence", "run"]
