"""Checks that need an ordering. All of them gate on an established time axis."""

from __future__ import annotations

from dataassay.checks.base import (
    BOOKKEEPING,
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
from dataassay.columns import q

MIN_POINTS_FOR_CADENCE = 12
FORECAST_FLAG_NAMES = ("is_forecast", "forecast", "is_projection", "projected",
                       "is_estimate", "scenario")
# A stamp recording when the row was WRITTEN. Useless as a time axis and
# invaluable as a reference for how far behind the observations are.
_RUN_STAMPS = ("run_date", "load_date", "ingested_at", "created_at",
               "fetched_at", "updated_at", "etl_date")


def _needs_time_axis(ctx: CheckContext) -> Applicability | None:
    s = ctx.structure
    if s.time_axis:
        return None
    if s.time_candidates:
        return Applicability.needs_answer(
            "Several temporal columns and no way to choose between them "
            f"({', '.join(s.time_candidates)}). Which one is the observation "
            "date?"
        )
    return Applicability.no("no temporal column, so the table has no ordering")


def _series_sql(ctx: CheckContext, value_col: str | None = None) -> str:
    s = ctx.structure
    cols = [f"{s.time_expr} AS t"]
    cols += [q(g) for g in s.group_columns]
    if value_col:
        cols.append(f"{q(value_col)} AS v")
    # NaN and Inf are dropped alongside the nulls, and for the same reason: a
    # check reads a series to measure it, and neither is a measurement. Left
    # in, they do not just skip a row -- every arithmetic comparison against
    # NaN is false, so a step becomes "x nan" and a threshold silently stops
    # rejecting anything. The profile still counts and reports them; this is
    # only about what the checks are asked to reason over.
    where = (
        f" WHERE {q(value_col)} IS NOT NULL AND isfinite({q(value_col)})"
        if value_col else ""
    )
    return f"SELECT {', '.join(cols)} FROM {ctx.source}{where}"


class FutureDates:
    spec = CheckSpec(
        id="future_dates",
        name="Rows dated after today",
        detects="Observations stamped with a date that has not happened yet.",
        gate="An established time axis.",
        default_disposition=SUSPECT,
        not_the_obvious=(
            "Flagging every future date is wrong: forecast and projection "
            "series legitimately extend forward, and a tool that cannot tell "
            "the two apart is unusable on half of a weather or energy file. "
            "What separates them is HOW FAR. A forecast runs out to a horizon; "
            "an off-by-one stamp lands exactly one period ahead."
        ),
        traces_to=(
            "M19/M20: a weekly price series stamped rows with the Friday that "
            "ends the current week rather than the last actual trading day, so "
            "the freshness monitor read data that did not exist yet as current."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        return _needs_time_axis(ctx) or Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        axis = ctx.structure.time_axis
        rows = ctx.fetch(
            f"SELECT count(*), max({ctx.structure.time_expr}), "
            f"date_diff('day', current_date, max(CAST({ctx.structure.time_expr} AS DATE))) "
            f"FROM {ctx.source} WHERE CAST({ctx.structure.time_expr} AS DATE) > current_date"
        )
        count, furthest, days_ahead = rows[0]
        if not count:
            return []

        declared_flag = (
            ctx.manifest.declared_value("forecast_column") if ctx.manifest else None
        )
        has_flag = [declared_flag] if declared_flag else [
            c.name for c in ctx.profile.columns
            if c.name.lower() in FORECAST_FLAG_NAMES
        ]
        cadence_days = _modal_gap_days(ctx)
        one_period_out = (
            cadence_days is not None and days_ahead is not None
            and 0 < days_ahead <= cadence_days
        )

        if has_flag:
            disposition, level = BOOKKEEPING, HIGH
            inputs = [f"{has_flag[0]!r} column present: this series carries "
                      "forecast rows by design"]
            summary = (
                f"{count:,} row(s) are dated ahead of today, out to {furthest}. "
                f"The {has_flag[0]!r} column says that is intended — but any "
                "consumer that ignores the flag will treat forecasts as actuals."
            )
        elif one_period_out:
            disposition, level = DEFECT, HIGH
            inputs = [
                f"the furthest row is {days_ahead} day(s) ahead, within one "
                f"cadence step ({cadence_days} day(s))",
                "no forecast flag column exists",
                "a forecast runs to a horizon; one period out is an off-by-one "
                "period stamp",
            ]
            summary = (
                f"{count:,} row(s) are dated {days_ahead} day(s) ahead, exactly "
                "one period out, with nothing marking them as forecasts. This "
                "is the signature of stamping a row with the END of its period "
                "instead of the observation date."
            )
        else:
            disposition, level = SUSPECT, LOW
            inputs = [
                f"furthest row is {days_ahead} day(s) ahead",
                "no forecast flag column, but the horizon is longer than one "
                "period — consistent with a genuine forecast series",
            ]
            summary = (
                f"{count:,} row(s) run ahead of today, out to {furthest}. If "
                "this is a forecast series that is expected; if not, these rows "
                "describe observations that have not happened."
            )

        return [Finding(
            check_id=self.spec.id,
            column=axis,
            disposition=disposition,
            summary=summary,
            evidence={"rows": count, "furthest": str(furthest),
                      "days_ahead": days_ahead, "cadence_days": cadence_days,
                      "forecast_flag": has_flag[0] if has_flag else None},
            predicate=(
                f"SELECT count(*), max({ctx.structure.time_expr}) FROM <source> "
                f"WHERE CAST({ctx.structure.time_expr} AS DATE) > current_date"
            ),
            confidence=Confidence(level, inputs),
        )]


def _modal_gap_days(ctx: CheckContext) -> int | None:
    axis = ctx.structure.time_axis
    part = ctx.structure.partition
    order = f"{part} ORDER BY t" if part else "ORDER BY t"
    rows = ctx.fetch(
        f"SELECT gap, count(*) n FROM ("
        f"  SELECT date_diff('day', lag(CAST(t AS DATE)) OVER ({order}), "
        f"                   CAST(t AS DATE)) AS gap"
        f"  FROM ({_series_sql(ctx)})"
        f") WHERE gap IS NOT NULL AND gap > 0 GROUP BY gap ORDER BY n DESC LIMIT 1"
    )
    del axis
    return int(rows[0][0]) if rows else None


class CadenceGap:
    spec = CheckSpec(
        id="cadence_gap",
        name="Missing periods in a regular series",
        detects="Absent observations in a series that otherwise arrives on a fixed cadence.",
        gate="A time axis and enough points for a dominant cadence to exist.",
        default_disposition=SUSPECT,
        not_the_obvious=(
            "Comparing against a calendar produces a wall of false positives on "
            "any business-day series. The cadence has to be learned from the "
            "series itself, and a daily series that skips exactly Saturday and "
            "Sunday is not missing anything."
        ),
        traces_to=(
            "The partial-fetch class of failure: a run that writes fewer rows "
            "than it read leaves holes no row-count check notices."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        blocked = _needs_time_axis(ctx)
        if blocked:
            return blocked
        if ctx.profile.provenance.row_count < MIN_POINTS_FOR_CADENCE:
            return Applicability.no(
                f"fewer than {MIN_POINTS_FOR_CADENCE} rows: no cadence can be "
                "established, so nothing can be called a gap"
            )
        return Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        cadence = _modal_gap_days(ctx)
        if not cadence:
            return []
        part = ctx.structure.partition
        order = f"{part} ORDER BY t" if part else "ORDER BY t"

        # Carry the series key through. A gap belongs to ONE series, and a
        # finding that cannot say which one cannot be charted honestly -- the
        # union of every series has no hole where an individual series does.
        groups = ctx.structure.group_columns
        gsel = "".join(f", {q(g)}" for g in groups)
        rows = ctx.fetch(
            f"SELECT prev, t, gap, "
            f"       CASE WHEN {cadence} = 1 AND gap = 3 "
            f"                 AND dayofweek(CAST(prev AS DATE)) = 5 "
            f"            THEN true ELSE false END AS weekend{gsel} "
            f"FROM ("
            f"  SELECT lag(t) OVER ({order}) AS prev, t{gsel}, "
            f"         date_diff('day', lag(CAST(t AS DATE)) OVER ({order}), "
            f"                   CAST(t AS DATE)) AS gap"
            f"  FROM ({_series_sql(ctx)})"
            f") WHERE gap > {cadence} ORDER BY gap DESC LIMIT 200"
        )
        # A gap must imply a WHOLE missing period. Weekly data that slips a day
        # around a holiday produces an 8-day gap and has lost nothing; treating
        # that as a hole buries the one real gap under two dozen non-events.
        def periods_missing(gap: int) -> int:
            return max(0, round(gap / cadence) - 1)

        real = [r for r in rows if not r[3] and periods_missing(r[2])]
        if not real:
            return []

        worst = real[0]
        worst_series = list(worst[4:]) if len(worst) > 4 else []
        missing = sum(periods_missing(r[2]) for r in real)
        jitter = len([r for r in rows if not r[3]]) - len(real)
        weekend_skips = len([r for r in rows if r[3]])
        inputs = [
            f"cadence of {cadence} day(s) learned from the series itself, not "
            "from a calendar",
            f"{len(real)} gap(s) exceed it",
        ]
        if weekend_skips:
            inputs.append(
                f"{weekend_skips} weekend skip(s) excluded as normal for a "
                "business-day series"
            )
        if jitter:
            inputs.append(
                f"{jitter} gap(s) excluded as cadence jitter — longer than "
                "one period but not long enough to have lost one"
            )
        return [Finding(
            check_id=self.spec.id,
            column=ctx.structure.time_axis,
            disposition=SUSPECT,
            summary=(
                f"{len(real)} gap(s) in a {cadence}-day cadence, about "
                f"{missing:,} missing period(s). The largest runs from {worst[0]} "
                f"to {worst[1]} ({worst[2]} days)"
                + (f", in {' / '.join(str(v) for v in worst_series)}"
                   if worst_series else "")
                + "."
            ),
            evidence={
                "cadence_days": cadence,
                "gap_count": len(real),
                "missing_periods": missing,
                "weekend_skips_excluded": weekend_skips,
                "jitter_excluded": jitter,
                "largest": [str(worst[0]), str(worst[1]), int(worst[2])],
                "series": [str(v) for v in worst_series],
                "examples": [[str(r[0]), str(r[1]), int(r[2])] for r in real[:5]],
            },
            predicate=(
                f"SELECT prev, t, date_diff('day', prev, t) FROM (SELECT "
                f"lag({ctx.structure.time_expr}) OVER ({order}) prev, "
                f"{ctx.structure.time_expr} t FROM <source>) "
                f"WHERE date_diff('day', prev, t) > {cadence}"
            ),
            confidence=Confidence(MEDIUM, inputs),
        )]


class FlatlineTail:
    spec = CheckSpec(
        id="flatline_tail",
        name="Series ends in a run of identical values",
        detects="A feed that stopped updating while the pipeline kept writing the last value.",
        gate="A time axis and a numeric column with enough history to compare against.",
        default_disposition=SUSPECT,
        not_the_obvious=(
            "A freshness check on the file's timestamp says everything is fine "
            "here -- the rows are new, the values are stale. And a fixed "
            "threshold ('5 identical values is bad') is wrong for any series "
            "that legitimately sits still, so the run at the end is compared "
            "against the longest run the series has ever had before."
        ),
        traces_to=(
            "The staleness class this repo's freshness contract exists for: a "
            "through-date read from mtime or a run_date column lies whenever "
            "the file is rewritten with unchanged content."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        blocked = _needs_time_axis(ctx)
        if blocked:
            return blocked
        if not ctx.columns("numeric"):
            return Applicability.no("no numeric columns")
        if ctx.profile.provenance.row_count < MIN_POINTS_FOR_CADENCE:
            return Applicability.no(
                "too few rows to distinguish a stuck feed from a short series"
            )
        return Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        out = []
        groups = ctx.structure.group_columns
        gcols = ", ".join(q(g) for g in groups)
        # Gaps-and-islands: consecutive equal values share (row_number over the
        # series) - (row_number over the series restricted to that value).
        by_series = f"PARTITION BY {gcols} " if groups else ""
        by_value = f"PARTITION BY {gcols}, v " if groups else "PARTITION BY v "
        if not groups:
            by_value = "PARTITION BY v "
        group_by = f"{gcols}, v, island" if groups else "v, island"

        for col in ctx.columns("numeric"):
            if col.non_null < MIN_POINTS_FOR_CADENCE or col.distinct <= 1:
                continue
            runs = ctx.fetch(
                f"WITH s AS ({_series_sql(ctx, col.name)}), "
                f"n AS (SELECT *, "
                f"        row_number() OVER ({by_series}ORDER BY t) "
                f"      - row_number() OVER ({by_value}ORDER BY t) AS island "
                f"      FROM s) "
                f"SELECT count(*) AS run, min(t) AS from_t, max(t) AS to_t, "
                f"       any_value(v) AS val "
                f"FROM n GROUP BY {group_by} ORDER BY to_t DESC, run DESC"
            )
            if len(runs) < 2:
                continue
            tail = runs[0]
            longest_before = max((r[0] for r in runs[1:]), default=0)
            if tail[0] < 3 or tail[0] <= longest_before:
                continue

            out.append(Finding(
                check_id=self.spec.id,
                column=col.name,
                disposition=SUSPECT,
                summary=(
                    f"The last {tail[0]} observations are all {tail[3]}, from "
                    f"{tail[1]} to {tail[2]}. The longest identical run "
                    f"anywhere earlier in this series is {longest_before}."
                ),
                evidence={
                    "run_length": tail[0],
                    "value": str(tail[3]),
                    "from": str(tail[1]),
                    "to": str(tail[2]),
                    "longest_prior_run": longest_before,
                },
                predicate=(
                    f"SELECT {ctx.structure.time_expr}, {q(col.name)} "
                    f"FROM <source> ORDER BY {ctx.structure.time_expr} DESC "
                    f"LIMIT {tail[0] + 2}"
                ),
                confidence=Confidence(MEDIUM, [
                    f"tail run of {tail[0]} exceeds the series' own historical "
                    f"maximum of {longest_before}",
                    "threshold comes from this series, not a fixed constant",
                ]),
            ))
        return out


class FileOrder:
    spec = CheckSpec(
        id="file_order",
        name="Rows are not stored in time order",
        detects="A file whose physical row order does not follow its own time axis.",
        gate="An established time axis.",
        default_disposition=SUSPECT,
        not_the_obvious=(
            "Every value in the file can be correct and this still bites. It is "
            "not about the data, it is about the ORDER, and the order is what a "
            "great deal of code silently depends on -- tail(1), iloc[-1], "
            "'the last row', a chart that plots rows as it reads them.\n\n"
            "The sharpest form of the question is not how many pairs are out of "
            "sequence but whether the LAST row is the latest observation, "
            "because that is the one most consumers actually take.\n\n"
            "A panel breaks the naive version. A file holding two interleaved "
            "series and grouped by series is out of global time order by "
            "construction and perfectly fine, so the inversions are counted "
            "WITHIN each series too. Disorder inside a series is the defect; "
            "disorder only between them is just how the file is laid out."
        ),
        traces_to=(
            "H7: a cold-storage CSV sorted alphabetically by month name — APR, "
            "AUG, DEC, FEB, JAN — so every surface that read the last row served "
            "March as 'latest' while April was sitting in the file."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        blocked = _needs_time_axis(ctx)
        if blocked:
            return blocked
        if ctx.profile.provenance.row_count < 3:
            return Applicability.no("fewer than 3 rows: order is meaningless")
        return Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        expr = ctx.structure.time_expr
        # A bare window preserves scan order, which for a file read is the order
        # the rows are physically stored in.
        groups = ctx.structure.group_columns
        gsel = "".join(f", {q(g)}" for g in groups)
        within = (
            f"PARTITION BY {', '.join(q(g) for g in groups)} ORDER BY rn"
            if groups else "ORDER BY rn"
        )
        rows = ctx.fetch(
            f"WITH n AS (SELECT row_number() OVER () AS rn, {expr} AS t{gsel} "
            f"           FROM {ctx.source}), "
            f"d AS (SELECT t, lag(t) OVER (ORDER BY rn) AS prev, "
            f"             lag(t) OVER ({within}) AS prev_in_series FROM n) "
            f"SELECT count(*) FILTER (WHERE t < prev), "
            f"       count(*) FILTER (WHERE t < prev_in_series), "
            f"       (SELECT max(t) FROM n), "
            f"       (SELECT t FROM n ORDER BY rn DESC LIMIT 1), "
            f"       count(*) FROM d"
        )
        inversions, in_series, latest, last_row, total = rows[0]
        if not inversions:
            return []

        last_is_latest = last_row == latest
        share = inversions / max(total - 1, 1)
        grouped_panel = bool(groups) and not in_series

        disposition = SUSPECT if (grouped_panel or last_is_latest) else DEFECT

        inputs = [
            f"{inversions:,} of {total - 1:,} adjacent row pairs go backwards "
            f"in time ({share:.0%})",
        ]
        if grouped_panel:
            inputs.append(
                f"but every one of the {' × '.join(groups)} series is "
                "individually in order — the file is grouped by series, which "
                "is a layout, not a defect"
            )
        elif in_series:
            inputs.append(
                f"{in_series:,} of them are inside a single series, where "
                "order cannot be explained by layout"
            )
        if last_is_latest:
            inputs.append(
                "the last row is still the most recent observation, so "
                "'take the last row' happens to work here"
            )
        else:
            inputs.append(
                f"the last row is {last_row}, but the most recent observation "
                f"is {latest} — anything reading the final row gets the wrong "
                "record"
            )

        return [Finding(
            check_id=self.spec.id,
            column=None,
            disposition=disposition,
            summary=(
                (f"Rows are not stored in time order: {inversions:,} adjacent "
                 f"pair(s) move backwards. ")
                + ("Each series is individually in order, so this is the file's "
                   "layout rather than a sorting fault — but t"
                   if grouped_panel else "T")
                + (f"he file ends at {last_row} while the latest observation is "
                   f"{latest}, so anything that takes the last row — tail(1), "
                   "iloc[-1], a chart plotting rows as read — is serving the "
                   "wrong record."
                   if not last_is_latest else
                   "he final row is still the newest, so this is a presentation "
                   "problem rather than a wrong answer — for now.")
            ),
            evidence={"inversions": int(inversions),
                      "inversions_within_series": int(in_series),
                      "grouped_panel": grouped_panel, "rows": int(total),
                      "last_row": str(last_row), "latest": str(latest),
                      "last_is_latest": bool(last_is_latest)},
            predicate=(
                f"SELECT row_number() OVER () AS rn, {expr} AS t FROM <source> "
                f"-- then look for t decreasing as rn increases"
            ),
            confidence=Confidence(HIGH, inputs),
        )]


class StaleTail:
    spec = CheckSpec(
        id="stale_tail",
        name="The series stops well before the file was written",
        detects="A feed that quietly stopped arriving while the file kept being rewritten.",
        gate="A time axis, an establishable cadence, and a load stamp or today's date.",
        default_disposition=DEFECT,
        not_the_obvious=(
            "A freshness check on the FILE says everything is fine: it was "
            "written today. The rows are new; the observations are not.\n\n"
            "Comparing against today also only works on the day you look. "
            "Comparing against the file's own load stamp works on an archived "
            "copy months later, which is the difference between a check that "
            "can be run and one that can be re-run.\n\n"
            "The threshold cannot be a fixed number of periods. A daily series "
            "is routinely several days behind on a Monday, and \"three periods\" "
            "flags every business-day feed in existence. The bar is the series' "
            "OWN largest historical gap, doubled — a lag it has never taken "
            "before, rather than a lag someone decided was too long."
        ),
        traces_to=(
            "H14: a fetcher that was never scheduled left its file frozen at "
            "2026-05-07 and invisible to the freshness monitor, which was "
            "reading the file's timestamp rather than its contents."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        blocked = _needs_time_axis(ctx)
        if blocked:
            return blocked
        if ctx.profile.provenance.row_count < MIN_POINTS_FOR_CADENCE:
            return Applicability.no("too few rows to establish a cadence")
        return Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        cadence = _modal_gap_days(ctx)
        if not cadence:
            return []
        stamp_col = next(
            (c.name for c in ctx.profile.columns
             if c.kind == "temporal" and c.name.lower() in _RUN_STAMPS), None
        )
        reference = f"max({q(stamp_col)})" if stamp_col else "current_date"
        rows = ctx.fetch(
            f"SELECT max(CAST({ctx.structure.time_expr} AS DATE)), "
            f"       CAST({reference} AS DATE), "
            f"       date_diff('day', max(CAST({ctx.structure.time_expr} AS DATE)), "
            f"                 CAST({reference} AS DATE)) "
            f"FROM {ctx.source}"
        )
        last, ref, behind = rows[0]
        if behind is None or behind <= 0:
            return []
        periods = behind / cadence

        # The series' own worst gap is the bar. Anything it has done before is
        # not evidence that it has stopped.
        part = ctx.structure.partition
        order = f"{part} ORDER BY t" if part else "ORDER BY t"
        gap_rows = ctx.fetch(
            f"SELECT max(gap) FROM (SELECT date_diff('day', "
            f"  lag(CAST(t AS DATE)) OVER ({order}), CAST(t AS DATE)) AS gap "
            f"  FROM ({_series_sql(ctx)}))"
        )
        worst_gap = int(gap_rows[0][0] or 0)
        threshold = max(worst_gap * 2, cadence * 3)
        if behind <= threshold:
            return []

        against = (
            f"its own {stamp_col!r} stamp of {ref}" if stamp_col
            else f"today ({ref})"
        )
        return [Finding(
            check_id=self.spec.id,
            column=None,
            disposition=DEFECT if stamp_col and periods >= 3 else SUSPECT,
            summary=(
                f"The last observation is {last}, {behind:,} day(s) — about "
                f"{periods:.0f} periods of a {cadence}-day cadence — before "
                f"{against}. The file is being written; the data is not "
                "arriving."
            ),
            evidence={"last_observation": str(last), "reference": str(ref),
                      "days_behind": int(behind), "periods_behind": round(periods, 1),
                      "cadence_days": cadence, "stamp_column": stamp_col,
                      "worst_historical_gap_days": worst_gap,
                      "threshold_days": threshold},
            predicate=(
                f"SELECT max({ctx.structure.time_expr}), {reference} FROM <source>"
            ),
            confidence=Confidence(HIGH if stamp_col else MEDIUM, [
                f"cadence of {cadence} day(s) learned from the series",
                f"{behind:,} day(s) behind exceeds twice this series' own worst "
                f"historical gap ({worst_gap} day(s)) — a lag it has never taken",
                f"compared against {against}",
                ("the load stamp travels with the file, so this holds on an "
                 "archived copy too" if stamp_col else
                 "no load stamp in the file, so this is only true as of today"),
            ]),
        )]
