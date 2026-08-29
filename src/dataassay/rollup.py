"""Roll-up: audit the same file again at a coarser grain.

A panel hides a whole class of defect from a per-series audit. When one
partner stops reporting, every surviving series is individually clean --
correct cadence, no gaps, no level shift -- and only the total sags. Checking
segments can never see that, because the evidence is not in any segment. It is
in their sum.

So the same catalog runs again over an aggregate. Nothing about the checks
changes; only the source does, which is the whole reason this is possible at
all -- everything below the profiler binds to a `source` expression, so a
GROUP BY is as good a source as a file.

The one thing that is NOT free is the aggregate itself. Summing is a claim
about a column: that two rows of it can be added and mean something. That is
true of quantities and values and false of prices, rates, shares, and indices,
and no amount of looking at the numbers reveals which -- a column of unit
prices sums perfectly happily into nonsense. Additivity is semantic, so it is
declared or refused, never inferred from the values. Columns that cannot be
established as additive are dropped from the rolled-up view and the reason is
recorded, exactly as a withheld check would be.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import duckdb

from dataassay.columns import PERCENTISH_NAME, q
from dataassay.structure import Structure

# How a column combines across the key being dropped. The distinction is
# extensive vs intensive, and it is the only thing about a roll-up that can be
# wrong in a way the numbers never reveal.
#
# SUM is right for an amount -- tonnes, dollars, head, acres. Add two regions'
# exports and you have the exports of both regions, which is a real quantity.
#
# MEAN is right for everything else, and it is the DEFAULT, because a mean is
# never a category error. The mean of several series is always a valid series
# to look at; it is merely not always the most natural summary. A sum of
# temperatures is not a temperature, a sum of z-scores is not a z-score, and a
# sum of soil-moisture fractions is not anything at all -- so guessing SUM is
# destructive where guessing MEAN is only imprecise. When the tool cannot tell,
# it takes the option whose failure mode is "less informative" over the one
# whose failure mode is "invents a number".
SUM, MEAN, NONE = "sum", "mean", "none"
AGGREGATES = (SUM, MEAN, NONE)

# Amounts. A column named like one of these is a quantity OF something, and
# quantities add. Deliberately narrow: a name that is merely unfamiliar falls
# through to MEAN rather than being summed on a guess.
EXTENSIVE_NAME = re.compile(
    r"(^|_)(qty|quantity|volume|vol|amount|amt|count|n|num|total|sum|"
    r"tonnes|tons|tonnage|kg|lbs|pounds|bushels|bu|bags|barrels|bbl|"
    r"head|acres|hectares|ha|units|value|revenue|sales|receipts|"
    r"exports|imports|shipments|production|stocks_total)($|_)",
    re.IGNORECASE,
)

@dataclass(frozen=True)
class Measure:
    name: str
    agg: str
    basis: str

    @property
    def additive(self) -> bool:
        return self.agg == SUM

    @property
    def usable(self) -> bool:
        return self.agg in (SUM, MEAN)

    def expr(self, quoted: str) -> str:
        return f"{self.agg}({quoted})"


@dataclass
class Level:
    """One coarser grain: which keys survive, which were dropped, what summed."""

    key: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    time_columns: list[str] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    rows: int = 0
    source_rows: int = 0

    @property
    def name(self) -> str:
        return " × ".join(self.key) if self.key else "the file as a whole"

    @property
    def label(self) -> str:
        dropped = ", ".join(self.dropped)
        return f"summed over {dropped} → {self.name}"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "dropped": self.dropped,
            "time_columns": self.time_columns,
            "measures": {m.name: m.agg for m in self.measures},
            "excluded": [{"column": c, "reason": r} for c, r in self.excluded],
            "rows": self.rows,
            "source_rows": self.source_rows,
            "label": self.label,
        }


def classify_measures(profile, structure: Structure, manifest=None) -> list[Measure]:
    """Decide how each numeric column combines, and say why.

    A manifest answer always wins: how a column aggregates is exactly the kind
    of fact a person knows and the file does not state.
    """
    declared: dict[str, str] = {}
    if manifest is not None:
        for col, how in (manifest.declared_value("aggregate") or {}).items():
            if how in AGGREGATES:
                declared[col] = how
        # List shorthands, for the common case of correcting one column.
        for col in manifest.declared_value("additive") or []:
            declared[col] = SUM
        for col in manifest.declared_value("non_additive") or []:
            declared.setdefault(col, MEAN)

    reserved = set(structure.grain) | set(structure.time_parts)
    if structure.time_axis:
        reserved.add(structure.time_axis)

    out: list[Measure] = []
    for col in profile.columns:
        if col.kind != "numeric" or col.name in reserved:
            continue
        if col.name in declared:
            out.append(Measure(col.name, declared[col.name], "declared"))
        elif EXTENSIVE_NAME.search(col.name):
            out.append(Measure(
                col.name, SUM,
                "named like an amount, so regions add to a total",
            ))
        else:
            out.append(Measure(
                col.name, MEAN,
                "averaged, not summed — nothing in the name says this is an "
                "amount, and a sum of anything else (a temperature, an index, "
                "a z-score) is not a quantity at all. Declare it under "
                "'additive' in the manifest if it really does add",
            ))
    return out


def levels_for(structure: Structure, measures: list[Measure]) -> list[Level]:
    """Leave-one-out over the series key.

    Dropping ONE key at a time is the question an analyst actually asks -- does
    this still look right once partner is out of the way -- and it keeps the
    number of audits linear instead of exponential in the key width.
    """
    groups = list(structure.group_columns)
    if not groups:
        return []

    usable = [m for m in measures if m.usable]
    excluded = [(m.name, m.basis) for m in measures if not m.usable]
    if not usable:
        return []

    time_cols = list(structure.time_parts) or (
        [structure.time_axis] if structure.time_axis else []
    )

    return [
        Level(
            key=[g for g in groups if g != drop],
            dropped=[drop],
            time_columns=time_cols,
            measures=usable,
            excluded=excluded,
        )
        for drop in groups
    ]


def source_for(level: Level, base_source: str) -> str:
    """The aggregate, as a source expression the rest of the engine can read.

    The grouped key is unique by construction, which is worth noting: a
    duplicate at a rolled-up level is impossible, so that check correctly goes
    quiet rather than reporting a false all-clear.
    """
    keys = [q(c) for c in level.time_columns + level.key]
    aggs = [f"{m.expr(q(m.name))} AS {q(m.name)}" for m in level.measures]
    return (
        f"(SELECT {', '.join(keys + aggs)} FROM {base_source} "
        f"GROUP BY {', '.join(keys)})"
    )


def measure(
    level: Level,
    con: duckdb.DuckDBPyConnection,
    source: str,
    params: list[str],
) -> tuple[list[tuple[str, str]], int]:
    """Schema and row count of the aggregate, without materializing it."""
    n = max(1, source.count("?"))
    described = con.execute(
        f"DESCRIBE SELECT * FROM {source}", params * n
    ).fetchall()
    (rows,) = con.execute(
        f"SELECT count(*) FROM {source}", params * n
    ).fetchone()
    return [(r[0], r[1]) for r in described], int(rows)


__all__ = [
    "Level", "Measure", "classify_measures", "levels_for", "measure",
    "source_for", "EXTENSIVE_NAME", "SUM", "MEAN", "NONE", "AGGREGATES",
]
