"""What a check is, and what it is allowed to claim.

Three ideas carry the design:

  A check is GATED. It declares the properties it needs, and the engine refuses
  to run it when the profile has not established them. A sigma rule on a
  tail-inflated column is not a weak check, it is an invalid one, and the way to
  stop it producing confident nonsense is to never let it run.

  A check that could not run is REPORTED. Withheld and blocked checks travel
  beside the findings, because "seasonality undetermined -- 1.2 cycles of
  history" is something the reader needs in order to know what the silence
  means.

  A finding carries its own PREDICATE. Whatever produced it must be re-runnable
  by the person reading it, without our code. An audit nobody can reproduce is
  an opinion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import duckdb

    from dataassay.columns import ColumnProfile
    from dataassay.profile import Profile
    from dataassay.structure import Structure

# -- disposition ---------------------------------------------------------------
# Most anomalies in real data are the source's own bookkeeping. A tool that
# cannot say so gets ignored within a week, so the disposition is part of the
# finding rather than left to the reader.
DEFECT = "defect"            # very likely wrong
SUSPECT = "suspect"          # wrong-looking, but a benign explanation exists
BOOKKEEPING = "bookkeeping"  # the source doing something legitimate

SEVERITY_ORDER = {DEFECT: 0, SUSPECT: 1, BOOKKEEPING: 2}

# -- confidence ----------------------------------------------------------------
HIGH = "high"
MEDIUM = "medium"
LOW = "low"

_CONFIDENCE_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}


@dataclass(frozen=True)
class Confidence:
    """Never an opaque score. Each input is named and shown.

    Agreement between independent checks is the strongest signal available and
    is applied by the engine after all checks have run -- a point flagged by two
    unrelated detectors is real in a way that one 3-sigma hit never is.
    """

    level: str
    inputs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"level": self.level, "inputs": list(self.inputs)}

    def corroborated_by(self, other_check: str) -> Confidence:
        promoted = HIGH if self.level == MEDIUM else self.level
        return Confidence(
            level=promoted,
            inputs=[*self.inputs, f"independently flagged by {other_check}"],
        )


@dataclass(frozen=True)
class Finding:
    check_id: str
    column: str | None
    disposition: str
    summary: str
    evidence: dict
    predicate: str
    confidence: Confidence
    # Set when `evidence` carries actual cell values. Acting on a finding
    # usually requires seeing the offending key, so the report shows them -- but
    # anything crossing a network boundary later has to know they are in there.
    raw_values: bool = False

    @property
    def sort_key(self) -> tuple[int, int]:
        return (
            SEVERITY_ORDER.get(self.disposition, 9),
            _CONFIDENCE_ORDER.get(self.confidence.level, 9),
        )

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "column": self.column,
            "disposition": self.disposition,
            "summary": self.summary,
            "evidence": self.evidence,
            "predicate": self.predicate,
            "confidence": self.confidence.to_dict(),
            "raw_values": self.raw_values,
        }


@dataclass(frozen=True)
class Applicability:
    """Why a check will or will not run here.

    `blocked` distinguishes the two kinds of no. A withheld check is impossible
    on this data and nothing can change that; a blocked one is waiting on an
    answer, and so belongs in the interview queue rather than the limitations
    list.
    """

    applicable: bool
    reason: str = ""
    blocked: bool = False

    @staticmethod
    def yes() -> Applicability:
        return Applicability(True)

    @staticmethod
    def no(reason: str) -> Applicability:
        return Applicability(False, reason)

    @staticmethod
    def needs_answer(question: str) -> Applicability:
        return Applicability(False, question, blocked=True)


@dataclass(frozen=True)
class CheckSpec:
    """The catalog entry. This is the part that is worth more than the code.

    `not_the_obvious` exists because the most expensive lesson in this corpus is
    that the obvious detector is often the wrong one: counting states finds
    nothing in a panel where absence is encoded as zero, and watching zeros move
    finds nothing in a ragged panel. Where a check has a near neighbour that
    fails, the catalog says so.
    """

    id: str
    name: str
    detects: str
    gate: str
    default_disposition: str
    not_the_obvious: str = ""
    traces_to: str = ""  # the real finding that earned this check a place

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "detects": self.detects,
            "gate": self.gate,
            "default_disposition": self.default_disposition,
            "not_the_obvious": self.not_the_obvious,
            "traces_to": self.traces_to,
        }


@dataclass
class CheckContext:
    """Everything a check is allowed to look at.

    The connection is shared: checks run against one open handle over the same
    source, so a file larger than memory is not re-opened per check.
    """

    profile: Profile
    structure: Structure
    con: duckdb.DuckDBPyConnection
    source: str
    params: list[str]

    def columns(self, kind: str | None = None) -> list[ColumnProfile]:
        cols = self.profile.columns
        return [c for c in cols if kind is None or c.kind == kind]

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.profile.columns if c.name == name), None)

    def holds(self, column: str, prop: str) -> bool:
        """Has the profile established `prop` for this column?"""
        col = self.column(column)
        if col is None:
            return False
        return any(
            p["property"] == prop and p["holds"] for p in col.observed_properties()
        )

    def fetch(self, sql: str) -> list[tuple]:
        """Run SQL against the source, binding the path once per reference.

        A check that names the source twice needs the parameter twice, and
        forgetting that surfaces as an opaque binder error rather than a wrong
        answer -- so the repetition is handled here instead of in every check.
        """
        placeholders = sql.count("?")
        params = self.params * max(1, placeholders // max(1, len(self.params)))
        return self.con.execute(sql, params).fetchall()


class Check(Protocol):
    spec: CheckSpec

    def applies(self, ctx: CheckContext) -> Applicability:
        """Decide from the profile and structure alone, before touching data."""
        ...

    def run(self, ctx: CheckContext) -> list[Finding]:
        ...
