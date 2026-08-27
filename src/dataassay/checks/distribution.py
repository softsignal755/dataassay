"""Checks about the shape of the values themselves."""

from __future__ import annotations

from dataassay.checks.base import (
    DEFECT,
    HIGH,
    LOW,
    MEDIUM,
    SUSPECT,
    Applicability,
    CheckContext,
    CheckSpec,
    Confidence,
    Finding,
)
from dataassay.checks.temporal import _needs_time_axis, _series_sql
from dataassay.columns import q

# A candidate step must clear BOTH bars.
#
# The MAD bar alone is not enough, and finding that out cost a rewrite. Market
# series have fat-tailed steps: on a positioning file, an 8-MAD move that holds
# for ten weeks is a perfectly ordinary regime change, and scoring against
# typical steps flagged 26 of them as defects. So a candidate must also be
# extreme among the series' ALREADY extreme steps -- an outlier in the tail, not
# just outside the body.
# The tail quantile must sit BELOW the outliers being hunted. At the 99th
# percentile a series with a couple of big jumps in a couple of hundred steps
# puts those jumps INTO the quantile, and the bar rises out of its own reach --
# the check then finds nothing at all. The 95th leaves them outside it.
STEP_MAD_MULTIPLE = 20.0
STEP_TAIL_QUANTILE = 0.95
STEP_TAIL_MULTIPLE = 4.0
MAD_TO_SIGMA = 1.4826
CONTEXT_WINDOW = 10
MIN_POINTS_FOR_STEPS = 20

# Ratios a units or basis change lands on. A market does not move by exactly a
# thousand; a units change does. Tolerance is 1%.
UNIT_RATIOS = {
    1000.0: "a factor of 1000 — a units change (e.g. tonnes to kilograms)",
    100.0: "a factor of 100 — a units or percentage-basis change",
    10.0: "a factor of 10 — a units change",
    2.20462: "the pounds-per-kilogram ratio",
    2204.62: "the pounds-per-tonne ratio",
    1.10231: "the short-tons-per-tonne ratio",
    35.3147: "the cubic-feet-per-cubic-metre ratio",
    6.28981: "the barrels-per-cubic-metre ratio",
}
UNIT_TOLERANCE = 0.01

SATURATION_SHARE = 0.05
MIN_N_FOR_SATURATION = 30
# Below this, a numeric column is a category wearing a number's clothes.
# 'dekad' takes the values 1, 2, 3; a third of the rows sitting on 1 is
# what that column IS, not a cap it ran into.
MIN_DISTINCT_FOR_SATURATION = 20
# Bounds a clipped series lands on. A cap at an arbitrary number is usually the
# data; a cap at exactly 100 or 1 is usually the code.
ROUND_BOUNDS = (0.0, 1.0, 100.0, 10.0, 1000.0, -1.0, -100.0)


class SaturationAtBound:
    spec = CheckSpec(
        id="saturation_at_bound",
        name="Values piled up on a bound",
        detects="A column where many values sit at exactly its maximum or minimum.",
        gate=(
            "A numeric column with enough values, and enough distinct values to "
            "be a measurement rather than an encoded category."
        ),
        default_disposition=SUSPECT,
        not_the_obvious=(
            "An outlier check looks for values far from the middle and finds "
            "nothing: a saturated column has no outliers, because everything "
            "extreme was clipped to the same number. The tell is a spike in "
            "MASS at one end, not distance from the centre.\n\n"
            "A pile-up at a zero FLOOR is excluded, and that exclusion matters "
            "more than the rest of the check. Zero is the natural bottom of any "
            "non-negative quantity -- a month with no cottonseed crushed is a "
            "real zero, not a clipped one -- and treating it as saturation "
            "flags most sparse columns in existence. (Whether those zeros mean "
            "'none' or 'not reported' is a separate question this check does "
            "not answer.)"
        ),
        traces_to=(
            "C6: a component summed alert area over all history with no date "
            "filter, so its 60%-weighted contribution pegged at 100 for every "
            "origin. The index then moved only with rescaled weather noise, and "
            "every origin read 'Critical' forever."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        usable = [
            c for c in ctx.columns("numeric")
            if c.non_null >= MIN_N_FOR_SATURATION
            and c.distinct >= MIN_DISTINCT_FOR_SATURATION
        ]
        if not usable:
            return Applicability.no(
                f"no numeric column has {MIN_N_FOR_SATURATION}+ values and "
                f"{MIN_DISTINCT_FOR_SATURATION}+ distinct values — the rest are "
                "categories encoded as numbers, where a pile-up means nothing"
            )
        return Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        out = []
        for col in ctx.columns("numeric"):
            if (col.non_null < MIN_N_FOR_SATURATION
                    or col.distinct < MIN_DISTINCT_FOR_SATURATION):
                continue
            for end, bound in (("maximum", col.max_value), ("minimum", col.min_value)):
                if bound is None:
                    continue
                # The zero floor of a non-negative column is where real zeros
                # live, not evidence of a cap. Tested with a tolerance: an
                # interpolated column's zeros arrive as -7.3e-22, and an exact
                # comparison lets those through as a "bound".
                scale = max(abs(float(col.max_value or 0)),
                            abs(float(col.min_value or 0)), 1.0)
                if end == "minimum" and abs(float(bound)) < 1e-9 * scale:
                    continue
                (hits,) = ctx.fetch(
                    f"SELECT count(*) FROM {ctx.source} WHERE {q(col.name)} = {bound}"
                )[0]
                share = hits / col.non_null
                if share < SATURATION_SHARE or hits < 2:
                    continue
                round_bound = any(
                    abs(float(bound) - b) <= 1e-9 * max(1.0, abs(b))
                    for b in ROUND_BOUNDS
                )
                inputs = [
                    f"{share:.1%} of values sit at exactly {bound}",
                    f"the column has {col.distinct:,} distinct values overall, "
                    "so this is a pile-up rather than a constant",
                ]
                if round_bound:
                    inputs.append(
                        f"{bound} is a round bound — a cap imposed by code more "
                        "often than a number produced by measurement"
                    )
                out.append(Finding(
                    check_id=self.spec.id,
                    column=col.name,
                    disposition=DEFECT if round_bound and share > 0.5 else SUSPECT,
                    summary=(
                        f"{hits:,} of {col.non_null:,} values ({share:.1%}) sit "
                        f"at exactly the {end}, {bound}. Values are being "
                        "clipped there, or a computation is saturating."
                    ),
                    evidence={"bound": str(bound), "end": end, "rows": hits,
                              "share": round(share, 4),
                              "round_bound": round_bound},
                    predicate=(
                        f"SELECT count(*) FROM <source> WHERE {q(col.name)} = {bound}"
                    ),
                    confidence=Confidence(
                        HIGH if round_bound and share > 0.5 else MEDIUM, inputs
                    ),
                ))
        return out


class LevelShift:
    spec = CheckSpec(
        id="level_shift",
        name="Discontinuity in a series",
        detects=(
            "A step so large it does not belong to the series, separated into a "
            "shift that persists (a splice or a units change) and one that "
            "reverts (a spike)."
        ),
        gate=(
            "A time axis, a numeric column with enough ordered points, and a "
            "non-zero robust scale for its steps."
        ),
        default_disposition=SUSPECT,
        not_the_obvious=(
            "Scoring the LEVEL is the obvious version and it is wrong twice "
            "over. A price series is not stationary, so its own history is not "
            "a distribution to score against; and standard deviation is "
            "inflated by exactly the jumps being looked for. Steps are scored, "
            "on a log scale where the series is strictly positive so a "
            "proportional move is the same size everywhere, against a MAD-based "
            "scale that the jumps themselves cannot inflate.\\n\\n"
            "The second half matters as much: a big step alone does not "
            "distinguish a units change from a bad tick. Whether the new level "
            "HOLDS does."
        ),
        traces_to=(
            "The splice class: a CSV whose basis changes at a fixed date, and "
            "the sign-convention inversion where a derived series flipped "
            "meaning partway through its own history."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        blocked = _needs_time_axis(ctx)
        if blocked:
            return blocked
        usable = [
            c for c in ctx.columns("numeric")
            if c.non_null >= MIN_POINTS_FOR_STEPS and c.distinct > 2
        ]
        if not usable:
            return Applicability.no(
                f"no numeric column has {MIN_POINTS_FOR_STEPS}+ ordered points"
            )
        return Applicability.yes()

    def _candidates(self, ctx: CheckContext, col) -> list[tuple]:
        use_log = col.min_value is not None and float(col.min_value) > 0
        expr = "ln(v)" if use_log else "v"
        part = ctx.structure.partition
        win = f"{part} ORDER BY t" if part else "ORDER BY t"
        # The group columns have to survive into every CTE the window
        # partitions on, or the partition clause binds to nothing.
        carry = "".join(f"{q(g)}, " for g in ctx.structure.group_columns)
        return ctx.fetch(
            f"WITH s AS ({_series_sql(ctx, col.name)}), "
            f"y AS (SELECT t, {carry}{expr} AS y FROM s), "
            f"d AS (SELECT t, {carry}y, y - lag(y) OVER ({win}) AS step, "
            f"        quantile_cont(y, 0.5) OVER ({win} "
            f"          ROWS BETWEEN {CONTEXT_WINDOW} PRECEDING AND 1 PRECEDING"
            f"        ) AS before_med, "
            f"        quantile_cont(y, 0.5) OVER ({win} "
            f"          ROWS BETWEEN CURRENT ROW AND {CONTEXT_WINDOW - 1} FOLLOWING"
            f"        ) AS after_med "
            f"      FROM y), "
            f"st AS (SELECT * FROM d WHERE step IS NOT NULL), "
            f"med AS (SELECT median(step) AS m FROM st), "
            f"sc AS (SELECT median(abs(st.step - med.m)) * {MAD_TO_SIGMA} AS mad, "
            f"              quantile_cont(abs(st.step - med.m), {STEP_TAIL_QUANTILE}) AS tail "
            f"       FROM st, med) "
            f"SELECT st.t, st.step, st.before_med, st.after_med, sc.mad, sc.tail "
            f"FROM st, med, sc "
            f"WHERE sc.mad > 0 "
            f"  AND abs(st.step - med.m) > {STEP_MAD_MULTIPLE} * sc.mad "
            f"  AND abs(st.step - med.m) > {STEP_TAIL_MULTIPLE} * sc.tail "
            f"ORDER BY abs(st.step - med.m) DESC LIMIT 10"
        )

    @staticmethod
    def _unit_ratio(step: float) -> str | None:
        """Does the jump land on a ratio a units change would produce?"""
        ratio = pow(2.718281828459045, abs(step))
        for value, label in UNIT_RATIOS.items():
            if abs(ratio - value) / value <= UNIT_TOLERANCE:
                return label
        return None

    def run(self, ctx: CheckContext) -> list[Finding]:
        # Gather across all columns first. A splice is a property of the FILE --
        # the source changed and every series moved on the same date. A regime
        # change is a property of one series. Nothing can tell them apart one
        # column at a time.
        gathered: list[tuple] = []
        numeric = [
            c for c in ctx.columns("numeric")
            if c.non_null >= MIN_POINTS_FOR_STEPS and c.distinct > 2
        ]
        for col in numeric:
            for row in self._candidates(ctx, col):
                gathered.append((col, *row))

        if not gathered:
            return []

        dates_hit: dict[str, set[str]] = {}
        for col, t, *_ in gathered:
            dates_hit.setdefault(str(t), set()).add(col.name)
        wide_threshold = max(2, (len(numeric) + 1) // 2)

        out: list[Finding] = []
        suppressed: set[tuple[str, str]] = set()

        for col, t, step, before, after, mad, tail in gathered:
            if (col.name, str(t)) in suppressed or before is None or after is None:
                continue
            use_log = col.min_value is not None and float(col.min_value) > 0
            moved = after - before
            persists = abs(moved) >= 0.5 * abs(step) and moved * step > 0

            # A spike appears twice: once going out, once coming back.
            if not persists:
                for other in gathered:
                    if (other[0] is col and other[1] != t
                            and other[2] * step < 0):
                        suppressed.add((col.name, str(other[1])))
                        break

            unit = self._unit_ratio(step) if use_log else None
            simultaneous = dates_hit.get(str(t), set())
            file_wide = len(simultaneous) >= wide_threshold

            magnitude = (
                f"×{pow(2.718281828459045, abs(step)):,.1f}" if use_log
                else f"{abs(step):,.4g}"
            )
            scale_note = (
                "steps scored on a log scale (strictly positive column, so a "
                "proportional move counts the same at any level)"
                if use_log else
                "steps scored as plain differences (the column reaches zero or "
                "below, so a log scale does not apply)"
            )
            inputs = [
                scale_note,
                f"clears both bars: {STEP_MAD_MULTIPLE:.0f}x the robust step "
                f"scale AND {STEP_TAIL_MULTIPLE:.0f}x the "
                f"{STEP_TAIL_QUANTILE:.0%} step size, so it is extreme among "
                "this series' already-extreme moves",
            ]

            if not persists:
                disposition, level = SUSPECT, MEDIUM
                summary = (
                    f"At {t} the series steps by {magnitude} and returns to "
                    "where it was — a single bad observation rather than a "
                    "change in the series."
                )
                inputs.append("the level reverts within "
                              f"{CONTEXT_WINDOW} observations")
            elif unit:
                disposition, level = DEFECT, HIGH
                summary = (
                    f"At {t} the series jumps by {magnitude} and holds — {unit}. "
                    "That is a change in how the quantity is expressed partway "
                    "through the history, not a movement in the quantity itself."
                )
                inputs.append(f"the jump ratio matches {unit} to within 1%")
            elif file_wide:
                disposition, level = DEFECT, HIGH
                summary = (
                    f"At {t} this series jumps by {magnitude} and holds — and so "
                    f"do {len(simultaneous) - 1} other column(s) on the same "
                    "date. A whole file changing level at once is the source "
                    "changing, not the market."
                )
                inputs.append(
                    f"{len(simultaneous)} of {len(numeric)} numeric columns "
                    f"break at {t} simultaneously"
                )
            else:
                disposition, level = SUSPECT, LOW
                summary = (
                    f"At {t} the series steps by {magnitude} and the new level "
                    "holds. No other column breaks on that date and the jump is "
                    "not a round ratio, so this may well be the series itself "
                    "rather than the data — worth an eye, not an alarm."
                )
                inputs.append(
                    "no corroboration: not a units ratio, and no other column "
                    "moves on this date"
                )

            out.append(Finding(
                check_id=self.spec.id,
                column=col.name,
                disposition=disposition,
                summary=summary,
                evidence={
                    "at": str(t),
                    "step": round(float(step), 6),
                    "log_scale": use_log,
                    "before_median": round(float(before), 6),
                    "after_median": round(float(after), 6),
                    "robust_step_scale": round(float(mad), 8),
                    "step_tail_quantile": round(float(tail), 8),
                    "persists": persists,
                    "unit_ratio": unit,
                    "columns_breaking_here": sorted(simultaneous),
                },
                predicate=(
                    f"SELECT {q(ctx.structure.time_axis)}, {q(col.name)} "
                    f"FROM <source> ORDER BY {q(ctx.structure.time_axis)} "
                    f"-- inspect the {CONTEXT_WINDOW} rows either side of {t}"
                ),
                confidence=Confidence(level, inputs),
            ))
        return out
