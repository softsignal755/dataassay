"""Orchestration: profile, infer structure, gate the checks, rank what survives.

Coverage is a first-class output, not a footnote. A reader cannot interpret an
empty findings list without knowing how many checks ran, which were withheld,
and which are waiting on an answer -- zero findings at 30% coverage and zero at
95% are entirely different objects, and one number cannot tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import duckdb

from dataassay import manifest as manifest_mod
from dataassay import profile as profile_mod
from dataassay import rollup as rollup_mod
from dataassay import structure as structure_mod
from dataassay.checks.base import Confidence, Finding
from dataassay.checks.registry import CATALOG, CATALOG_VERSION
from dataassay.profile import Profile, build
from dataassay.provenance import Column, reader_for, source_expr
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
    manifest_path: str | None = None
    # Question codes a person has seen and deliberately declined to answer.
    # An unanswered question and a declined one are different states and the
    # report must not show them as the same thing: the first is work nobody has
    # done yet, the second is a decision, and presenting a decision as an
    # outstanding task is how a report trains people to ignore it.
    skipped_questions: list[str] = field(default_factory=list)
    # Set when this Audit describes an AGGREGATE of the file rather than the
    # file itself. The provenance stays the file's -- the same bytes produced
    # it -- but a reader must never mistake a rolled-up finding for one about
    # a row that exists on disk.
    level: rollup_mod.Level | None = None
    rollups: list[Audit] = field(default_factory=list)
    rollup_withheld: str = ""

    @property
    def open_questions(self) -> list:
        return [q for q in self.profile.questions
                if q.code not in set(self.skipped_questions)]

    @property
    def declined_questions(self) -> list:
        return [q for q in self.profile.questions
                if q.code in set(self.skipped_questions)]

    def to_dict(self) -> dict:
        d = self.profile.to_dict()
        d |= {
            "catalog_version": self.catalog_version,
            "structure": self.structure.to_dict(),
            "coverage": self.coverage.to_dict(),
            "manifest": self.manifest_path,
            "skipped_questions": self.skipped_questions,
            "findings": [f.to_dict() for f in self.findings],
        }
        if self.level is not None:
            d["level"] = self.level.to_dict()
        if self.rollups:
            d["rollups"] = [r.to_dict() for r in self.rollups]
        if self.rollup_withheld:
            d["rollup_withheld"] = self.rollup_withheld
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


def _run_checks(ctx) -> tuple[Coverage, list[Finding]]:
    """Gate every check in the catalog against one context, and record both.

    Split out from `run` because a rolled-up level is audited by exactly the
    same catalog against exactly the same kind of context -- if a level needed
    its own check loop, the roll-up would not be the same audit and could not
    be compared with the one it came from.
    """
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
    return coverage, findings


def _only_new(found: list[Finding], already: set) -> list[Finding]:
    return [f for f in found if (f.check_id, f.column, f.summary) not in already]


def _run_rollups(
    ctx, profile: Profile, struct: Structure, manifest, requested: bool,
    base_findings: list[Finding],
) -> tuple[list[Audit], str]:
    """Audit the aggregate of each leave-one-out grain.

    Returns the reason instead of raising when no level can be built: "there is
    nothing to roll up" is an answer about the data, and silently returning an
    empty list would read as "rolled up and found nothing".
    """
    from dataassay.checks.base import CheckContext
    from dataassay.columns import profile_columns
    from dataassay.rawscan import RawScan

    if not requested:
        return [], ""
    if not struct.group_columns:
        return [], (
            "no series key was inferred, so every row is already its own "
            "series and there is no coarser grain to sum to"
        )

    measures = rollup_mod.classify_measures(profile, struct, manifest)
    levels = rollup_mod.levels_for(struct, measures)
    if not levels:
        refused = ", ".join(m.name for m in measures if not m.usable)
        return [], (
            "no numeric column can be aggregated"
            + (f" ({refused} refused)" if refused else "")
            + ", so there is nothing to carry to a coarser grain"
        )

    # What the file-level audit already said. A roll-up earns its place by
    # showing what per-series checking COULD NOT see, so a finding it restates
    # word for word is noise -- and repeated once per level, it is the kind of
    # noise that teaches people to skim the report.
    already = {(f.check_id, f.column, f.summary) for f in base_findings}

    out: list[Audit] = []
    for level in levels:
        source = rollup_mod.source_for(level, ctx.source)
        schema, rows = rollup_mod.measure(level, ctx.con, source, ctx.params)
        level.rows, level.source_rows = rows, profile.provenance.row_count
        if rows < 2:
            continue

        cols = profile_columns(ctx.con, source, ctx.params, schema, rows)
        prov = replace(
            profile.provenance, row_count=rows,
            columns=[Column(name=n, declared_type=d) for n, d in schema],
        )
        sub_profile = Profile(
            provenance=prov,
            rawscan=RawScan(
                applicable=False,
                reason="these rows are sums computed from the file, not bytes "
                       "in it, so there is no raw text to read",
            ),
            columns=cols,
            notes=profile_mod._column_notes(cols),
        )
        sub_struct = structure_mod.infer(
            cols, ctx.con, source, ctx.params, rows, manifest=None
        )
        sub_ctx = CheckContext(
            profile=sub_profile, structure=sub_struct, con=ctx.con,
            source=source, params=ctx.params, manifest=None, derived=True,
        )
        cov, found = _run_checks(sub_ctx)
        found = _corroborate(found)
        found = _only_new(found, already)
        # Accumulated, not reset: one fact about the whole file ("it stops in
        # December") is one fact at every grain, and printing it once per level
        # would bury the findings that are actually specific to a level.
        already |= {(f.check_id, f.column, f.summary) for f in found}
        found.sort(key=lambda f: f.sort_key)
        out.append(Audit(
            profile=sub_profile, structure=sub_struct, findings=found,
            coverage=cov, level=level,
        ))
    return out, ""


def run(
    path: Path,
    byte_cap: int | None = None,
    manifest_path: Path | None = None,
    use_manifest: bool = True,
    rollup: bool = False,
) -> Audit:
    from dataassay.checks.base import CheckContext
    from dataassay.rawscan import BYTE_CAP

    reader = reader_for(path)
    con = duckdb.connect(":memory:")
    try:
        profile = build(path, byte_cap=byte_cap or BYTE_CAP, con=con)
        source = source_expr(reader, profile.provenance.read_mode)
        params = [str(path)]
        manifest = (
            manifest_mod.discover(path, manifest_path) if use_manifest else None
        )
        struct = structure_mod.infer(
            profile.columns, con, source, params, profile.provenance.row_count,
            manifest=manifest,
        )
        ctx = CheckContext(
            profile=profile, structure=struct, con=con, source=source,
            params=params, manifest=manifest,
        )

        coverage, findings = _run_checks(ctx)
        findings = _corroborate(findings)
        rollups, rollup_withheld = _run_rollups(
            ctx, profile, struct, manifest, rollup, findings
        )
    finally:
        con.close()

    findings.sort(key=lambda f: f.sort_key)
    return Audit(
        profile=profile, structure=struct, findings=findings, coverage=coverage,
        manifest_path=(
            str(manifest.source_path) if manifest and manifest.source_path else None
        ),
        skipped_questions=list(manifest.skipped) if manifest else [],
        rollups=rollups,
        rollup_withheld=rollup_withheld,
    )


__all__ = ["Audit", "Coverage", "Confidence", "run"]
