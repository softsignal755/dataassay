"""Values that arithmetic produced and a source never did."""

from __future__ import annotations

from dataassay.checks.base import (
    DEFECT,
    HIGH,
    MEDIUM,
    SUSPECT,
    Applicability,
    CheckContext,
    CheckSpec,
    Confidence,
    Finding,
)
from dataassay.columns import q

# How many empty decades of magnitude separate residue from data. Six is far
# beyond anything a single quantity varies across in one column while still
# being one quantity.
GAP_DECADES = 6
MIN_N_FOR_RESIDUE = 20
# Residue repeats; measurements vary. A cluster of a handful of distinct values
# is one computation's output, not a range of very small readings.
TIGHT_CLUSTER = 5


class NegligibleResidue:
    spec = CheckSpec(
        id="negligible_residue",
        name="Values that should be zero but are not quite",
        detects=(
            "A cluster of values orders of magnitude below everything else in "
            "their own column, separated from it by a gap — arithmetic residue "
            "standing where a zero belongs."
        ),
        gate="A numeric column with enough non-zero values to see a distribution.",
        default_disposition=DEFECT,
        not_the_obvious=(
            "Flagging small values is the obvious version and it is wrong. Some "
            "columns legitimately span twenty decades — concentrations, "
            "probabilities, trace readings — and their smallest values are "
            "their most interesting ones.\\n\\n"
            "What separates residue from data is not smallness, it is the GAP. "
            "Real measurements form a continuum down to their limit; residue "
            "forms a spike with empty decades above it. A column holding 3,683 "
            "values at 1e-22, nothing at all for twelve decades, then a "
            "continuum from 1e-9 upward, is not measuring anything at 1e-22.\\n\\n"
            "Nor is this cosmetic. `column = 0` is FALSE for every one of those "
            "values, so zero-counts, joins, and GROUP BY quietly return the "
            "wrong answer while every type check, sum, and average accepts them."
        ),
        traces_to=(
            "Found by this tool in _history_coffee.csv: snow-water columns whose "
            "zeros arrive as -7.345364920210326e-22 — the same value 3,683 "
            "times, the fingerprint of one interpolation kernel over an all-zero "
            "input."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        usable = [
            c for c in ctx.columns("numeric")
            if c.non_null >= MIN_N_FOR_RESIDUE and c.distinct > 1
        ]
        if not usable:
            return Applicability.no(
                f"no numeric column has {MIN_N_FOR_RESIDUE}+ values and more "
                "than one distinct value"
            )
        return Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        out = []
        for col in ctx.columns("numeric"):
            if col.non_null < MIN_N_FOR_RESIDUE or col.distinct <= 1:
                continue
            rows = ctx.fetch(
                f"SELECT floor(log10(abs({q(col.name)})))::BIGINT AS decade, "
                f"       count(*) AS n, count(DISTINCT {q(col.name)}) AS d, "
                f"       max(abs({q(col.name)})) AS hi "
                f"FROM {ctx.source} "
                f"WHERE {q(col.name)} IS NOT NULL AND {q(col.name)} <> 0 "
                f"  AND isfinite({q(col.name)}) "
                f"GROUP BY 1 ORDER BY 1"
            )
            if len(rows) < 2:
                continue

            decades = [r[0] for r in rows]
            # The gap the whole check turns on: the first jump of GAP_DECADES or
            # more between occupied magnitudes.
            split = None
            for i in range(len(decades) - 1):
                if decades[i + 1] - decades[i] >= GAP_DECADES:
                    split = i
                    break
            if split is None:
                continue

            cluster = rows[: split + 1]
            count = sum(r[1] for r in cluster)
            distinct = sum(r[2] for r in cluster)
            top = max(r[3] for r in cluster)
            gap = decades[split + 1] - decades[split]
            tight = distinct <= TIGHT_CLUSTER

            inputs = [
                f"{count:,} value(s) at magnitude 1e{decades[split]} and below, "
                f"then {gap} empty decades before the next value at "
                f"1e{decades[split + 1]}",
                "the gap is what separates residue from data — a column that "
                "genuinely measures across many decades is a continuum",
            ]
            if tight:
                inputs.append(
                    f"only {distinct} distinct value(s) in the cluster: "
                    "residue repeats, measurements vary"
                )

            out.append(Finding(
                check_id=self.spec.id,
                column=col.name,
                disposition=DEFECT if tight else SUSPECT,
                summary=(
                    f"{count:,} value(s) sit at magnitudes up to {top:.3g}, "
                    f"separated by {gap} empty decades from the rest of the "
                    f"column. These are almost certainly meant to be zero — and "
                    f"`{col.name} = 0` is false for every one of them, so any "
                    "zero-count, join, or GROUP BY on this column is quietly "
                    "wrong."
                ),
                evidence={
                    "rows": count,
                    "distinct_in_cluster": distinct,
                    "cluster_max_magnitude": float(top),
                    "gap_decades": int(gap),
                    "next_decade_above": int(decades[split + 1]),
                    "tight_cluster": tight,
                },
                predicate=(
                    f"SELECT count(*) FROM <source> WHERE {q(col.name)} <> 0 "
                    f"AND abs({q(col.name)}) < 1e{decades[split] + 1}"
                ),
                confidence=Confidence(HIGH if tight else MEDIUM, inputs),
            ))
        return out
