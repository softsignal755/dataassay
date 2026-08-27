"""What shape the table is: which column is time, and what makes a row unique.

Almost every interesting check needs one or both. A gap check needs an ordering;
a duplicate check needs a grain; a level-shift check needs both. None of that is
in the profile, because none of it is a measurement -- it is an inference, and a
wrong one invalidates every check built on it.

So it is inferred where the data decides it, blocked where it does not, and the
reasoning is recorded either way. Assume and show; ask only when a wrong guess
would produce confident nonsense.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb

from dataassay.columns import ColumnProfile, q

# Names that conventionally mark the observation date, in preference order. Weak
# evidence, used only to break a tie between several temporal columns.
_TIME_NAMES = (
    "date", "as_of", "as_of_date", "observation_date", "period", "timestamp",
    "time", "dt", "day", "week", "month", "year",
)

# A column that records when the ROW was written, not when the observation
# happened. Never the time axis -- picking it silently turns every series into a
# flat line at the fetch date.
_RUN_NAMES = ("run_date", "load_date", "ingested_at", "created_at", "fetched_at",
              "updated_at", "_loaded", "etl_date")

MAX_GROUP_CARDINALITY = 200
MAX_GROUP_COLUMNS = 4

# Agricultural and government data very often spreads the observation date
# across columns -- year + month + dekad, or year + "END OF APR" -- instead of
# carrying a date. Treating those files as having no ordering costs five of the
# eleven checks, and it is why a cold-storage file sorted ALPHABETICALLY by
# month (APR, AUG, DEC, FEB, JAN...) could serve March as "latest" while April
# existed: there was no axis to notice it against.
_YEAR_NAMES = ("year", "yr", "crop_year", "harvest_year")
_MONTH_NAMES = ("month", "mon", "mo", "month_no", "month_num")
_DAY_NAMES = ("day", "dom", "day_of_month")
_DEKAD_NAMES = ("dekad", "decad", "tenday")
_MONTH_TOKENS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                 "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


@dataclass
class Structure:
    time_axis: str | None = None
    time_axis_basis: str = ""
    time_candidates: list[str] = field(default_factory=list)
    group_columns: list[str] = field(default_factory=list)
    grain: list[str] = field(default_factory=list)
    grain_is_unique: bool | None = None
    duplicate_grain_rows: int = 0
    candidates_exhausted: bool = False
    time_axis_declared: bool = False
    grain_declared: bool = False
    time_parts: list[str] = field(default_factory=list)
    time_sql: str = ""
    assumptions: list[str] = field(default_factory=list)

    @property
    def ordered(self) -> bool:
        return self.time_axis is not None

    @property
    def time_expr(self) -> str:
        """SQL for the observation date, whether it is a column or assembled."""
        if self.time_sql:
            return self.time_sql
        return q(self.time_axis) if self.time_axis else ""

    @property
    def partition(self) -> str:
        """SQL PARTITION BY clause for per-series windows, or empty."""
        if not self.group_columns:
            return ""
        return "PARTITION BY " + ", ".join(q(c) for c in self.group_columns)

    def to_dict(self) -> dict:
        return {
            "time_axis": self.time_axis,
            "time_axis_basis": self.time_axis_basis,
            "time_candidates": self.time_candidates,
            "group_columns": self.group_columns,
            "grain": self.grain,
            "grain_is_unique": self.grain_is_unique,
            "duplicate_grain_rows": self.duplicate_grain_rows,
            "candidates_exhausted": self.candidates_exhausted,
            "time_axis_declared": self.time_axis_declared,
            "time_parts": self.time_parts,
            "time_sql": self.time_sql,
            "grain_declared": self.grain_declared,
            "assumptions": self.assumptions,
        }


def _pick_time_axis(cols: list[ColumnProfile], s: Structure) -> None:
    temporal = [c.name for c in cols if c.kind == "temporal"]
    s.time_candidates = temporal

    observation = [c for c in temporal if c.lower() not in _RUN_NAMES]
    if len(temporal) > len(observation):
        dropped = sorted(set(temporal) - set(observation))
        s.assumptions.append(
            f"Ignored {', '.join(dropped)} as a time axis: the name says it "
            "records when the row was written, not when the observation "
            "happened."
        )

    if not observation:
        s.time_axis_basis = (
            "no temporal column" if not temporal
            else "the only temporal columns look like load stamps"
        )
        return

    if len(observation) == 1:
        s.time_axis = observation[0]
        s.time_axis_basis = "the only observation-dated column"
        return

    for preferred in _TIME_NAMES:
        for name in observation:
            if name.lower() == preferred:
                s.time_axis = name
                s.time_axis_basis = f"named {name!r}, chosen over {len(observation) - 1} other"
                s.assumptions.append(
                    f"Several temporal columns present ({', '.join(observation)}); "
                    f"used {name!r} as the time axis on the strength of its name alone."
                )
                return

    s.time_axis_basis = (
        f"{len(observation)} temporal columns and no name to choose between them: "
        + ", ".join(observation)
    )


def _month_case(col: str) -> str:
    """Map a period label carrying a month name to a month number."""
    whens = " ".join(
        f"WHEN upper({q(col)}) LIKE '%{tok}%' THEN {i + 1}"
        for i, tok in enumerate(_MONTH_TOKENS)
    )
    return f"CASE {whens} ELSE NULL END"


def _pick_composite_axis(cols: list[ColumnProfile], s: Structure) -> None:
    """Assemble a date from year + month (+ day or dekad), when one is spread out."""
    by_name = {c.name.lower(): c for c in cols}

    def numeric_named(names, lo, hi):
        for n in names:
            c = by_name.get(n)
            if (c and c.kind == "numeric" and c.integral
                    and c.min_value is not None and c.max_value is not None
                    and lo <= float(c.min_value) and float(c.max_value) <= hi):
                return c.name
        return None

    year = numeric_named(_YEAR_NAMES, 1800, 2200)
    if not year:
        return
    month = numeric_named(_MONTH_NAMES, 1, 12)
    month_sql, parts = None, [year]

    if month:
        month_sql, _ = q(month), parts.append(month)
    else:
        # A period label: "END OF APR", "MARCH", "Jan-2026".
        for c in cols:
            if c.kind != "text" or c.distinct > 40 or not c.top_values:
                continue
            sample = " ".join(str(v).upper() for v, _ in c.top_values)
            if sum(tok in sample for tok in _MONTH_TOKENS) >= 2:
                month_sql = _month_case(c.name)
                parts.append(c.name)
                break
    if not month_sql:
        return

    day = numeric_named(_DAY_NAMES, 1, 31)
    dekad = numeric_named(_DEKAD_NAMES, 1, 3)
    if day:
        parts.append(day)
        expr = f"make_date({q(year)}, {month_sql}, {q(day)})"
    elif dekad:
        parts.append(dekad)
        expr = (f"make_date({q(year)}, {month_sql}, 1) "
                f"+ to_days((({q(dekad)} - 1) * 10)::INTEGER)")
    else:
        expr = f"make_date({q(year)}, {month_sql}, 1)"

    s.time_axis = " + ".join(parts)
    s.time_parts = parts
    s.time_sql = expr
    s.time_axis_basis = "assembled from " + ", ".join(repr(x) for x in parts)
    s.assumptions.append(
        "No date column, so the observation date was assembled from "
        + ", ".join(repr(x) for x in parts)
        + ". Ordering, gaps and level shifts are read against that."
    )


def _pick_grain(
    cols: list[ColumnProfile],
    con: duckdb.DuckDBPyConnection,
    source: str,
    params: list[str],
    rows: int,
    s: Structure,
) -> None:
    """Find the series partition, and separately what makes a row unique.

    These are two different questions and conflating them costs the ordered
    checks their partition. A table can carry a unique surrogate id AND be a
    panel of (date x market) series; keying on the id satisfies uniqueness and
    leaves every series-aware check running over an interleaved mess.

    So the panel search always runs when there is a time axis, and a unique
    single column is only used as the grain when the panel search fails.
    """
    # A key is an identifier. A float measurement can be unique by accident --
    # 3,941 distinct temperatures in 3,941 rows is a coincidence of resolution,
    # not a key -- and calling it the grain is both wrong and useless.
    unique_cols = [
        c.name for c in cols
        if rows and c.distinct == c.non_null == rows
        and (c.kind in ("text", "temporal") or (c.kind == "numeric" and c.integral))
    ]

    if s.time_axis:
        candidates = [
            c.name for c in cols
            if c.name != s.time_axis and c.name not in s.time_parts
            and 1 < c.distinct <= MAX_GROUP_CARDINALITY
            # A grouping column repeats. One with a distinct value for nearly
            # every row is a surrogate key or a measurement, and bolting it on
            # reaches "uniqueness" while describing nothing.
            and c.distinct * 2 <= rows
            and (c.kind in ("text", "boolean")
                 or (c.kind == "numeric" and c.integral))
        ]
        candidates.sort(key=lambda n: next(c.distinct for c in cols if c.name == n))

        def distinct_of(keys: list[str]) -> int:
            cols_sql = ", ".join(q(k) for k in keys)
            (n,) = con.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT {cols_sql} FROM {source})",
                params,
            ).fetchone()
            return int(n)

        trial = list(s.time_parts) if s.time_parts else [s.time_axis]
        if distinct_of(trial) == rows:
            s.grain, s.grain_is_unique = trial, True
            return
        for extra in candidates[:MAX_GROUP_COLUMNS]:
            trial.append(extra)
            s.group_columns.append(extra)
            if distinct_of(trial) == rows:
                s.grain, s.grain_is_unique = list(trial), True
                return

        # The panel key was not found. A unique column still answers the
        # narrower question of what makes a row unique.
        if unique_cols:
            s.grain = [unique_cols[0]]
            s.grain_is_unique = True
            s.assumptions.append(
                f"No combination of {' × '.join(trial)} is unique, but "
                f"{unique_cols[0]!r} is unique and non-null across all "
                f"{rows:,} rows; used as the row key."
            )
            return

        s.grain = list(trial)
        s.grain_is_unique = False
        # We ran out of candidates without reaching uniqueness. That means the
        # key was not FOUND -- it does not mean duplicates were found.
        # Conflating the two turns every wide-keyed table into a fabricated
        # defect.
        s.candidates_exhausted = True
        cols_sql = ", ".join(q(k) for k in s.grain)
        (dupes,) = con.execute(
            f"SELECT coalesce(sum(n - 1), 0) FROM ("
            f"SELECT count(*) n FROM {source} GROUP BY {cols_sql} HAVING count(*) > 1)",
            params,
        ).fetchone()
        s.duplicate_grain_rows = int(dupes)
        return

    if unique_cols:
        s.grain = [unique_cols[0]]
        s.grain_is_unique = True
        s.assumptions.append(
            f"{unique_cols[0]!r} is unique and non-null across all {rows:,} "
            "rows; treated as the row key."
        )


def _apply_declared(s: Structure, manifest, cols, con, source, params, rows) -> None:
    """A declared value replaces an inference and says so.

    This is the point of the manifest: a grain nobody could infer becomes a
    grain the checks can use, and a duplicate check that could only ask a
    question can now answer one.
    """
    names = {c.name for c in cols}

    axis = manifest.declared_value("time_axis")
    if axis:
        if axis not in names:
            s.assumptions.append(
                f"Manifest declares time_axis {axis!r}, which is not a column "
                "in this file; ignored."
            )
        else:
            s.time_axis = axis
            s.time_axis_basis = "declared in the manifest"
            s.time_axis_declared = True

    grain = manifest.declared_value("grain")
    if grain:
        missing = [g for g in grain if g not in names]
        if missing:
            s.assumptions.append(
                f"Manifest declares a grain naming {', '.join(missing)}, which "
                "this file does not have; ignored."
            )
            return
        s.grain = list(grain)
        s.grain_declared = True
        s.candidates_exhausted = False
        s.group_columns = [
            g for g in grain if g != s.time_axis and g not in s.time_parts
        ]
        cols_sql = ", ".join(q(k) for k in grain)
        (distinct,) = con.execute(
            f"SELECT count(*) FROM (SELECT DISTINCT {cols_sql} FROM {source})", params
        ).fetchone()
        s.grain_is_unique = int(distinct) == rows
        if not s.grain_is_unique:
            (dupes,) = con.execute(
                f"SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) n FROM "
                f"{source} GROUP BY {cols_sql} HAVING count(*) > 1)",
                params,
            ).fetchone()
            s.duplicate_grain_rows = int(dupes)


def infer(
    cols: list[ColumnProfile],
    con: duckdb.DuckDBPyConnection,
    source: str,
    params: list[str],
    rows: int,
    manifest=None,
) -> Structure:
    s = Structure()
    _pick_time_axis(cols, s)
    if not s.time_axis:
        _pick_composite_axis(cols, s)
    if manifest is not None and manifest.declared_value("time_axis"):
        _apply_declared(s, manifest, cols, con, source, params, rows)
    if rows and not s.grain_declared:
        _pick_grain(cols, con, source, params, rows, s)
    if manifest is not None and manifest.declared_value("grain"):
        _apply_declared(s, manifest, cols, con, source, params, rows)
    if s.group_columns:
        s.assumptions.append(
            "Treated " + ", ".join(repr(c) for c in s.group_columns)
            + " as the series key: each combination is one series through time."
        )
    return s
