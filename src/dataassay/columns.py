"""Typed measurement, in one pass.

Everything here is a *measurement*, not a conclusion. Property establishment --
deciding that a column is a price level and therefore must be scored on log
steps, or that a panel is ragged rather than zero-filled -- gates the checks and
belongs with them. What this module produces is the evidence those decisions are
made from, and the payload an LLM would be shown (counts, quantiles, cardinality
-- never rows).

The aggregates for every column are assembled into a single SELECT so a file
larger than memory is streamed once rather than once per column. Top-k values
need a GROUP BY and so cost one extra pass each; they are only requested for
columns whose cardinality makes them meaningful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import duckdb

QUANTILES = [0.0, 0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99, 1.0]

# Values conventionally used to mean "missing" inside a numeric column, where
# they silently join the distribution instead of being excluded from it.
NUMERIC_SENTINELS = (-99999.0, -9999.0, -999.0, -99.0, 9999.0, 99999.0, 999999.0)

# Above this row count, exact distinct counts stop being worth their cost. Which
# method was used is recorded rather than hidden.
EXACT_DISTINCT_LIMIT = 2_000_000

TOPK_MAX_CARDINALITY = 50
TOPK_LIMIT = 10

# IQR of a normal distribution spans 1.349 sigma; the ratio of the classical to
# the robust scale is therefore ~1 for a well-behaved column and grows with the
# tail. Past this, a sigma-based outlier rule is not weak, it is invalid.
HEAVY_TAIL_RATIO = 2.0
MIN_N_FOR_SIGMA = 30

# Any column of counts or prices satisfies 0 <= x <= 100 somewhere, so the
# range alone is not worth a question. The name is weak evidence, but it is
# the only evidence available before the interview -- so it decides whether
# the observation is worth interrupting someone for, not whether it is true.
PERCENTISH_NAME = re.compile(r"(^|_)(pct|percent|pc|share|rate|ratio|util)"
                             r"|percent|_pct|pct_|%", re.IGNORECASE)

_NUMERIC_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
    "FLOAT", "DOUBLE", "REAL", "DECIMAL",
)
_TEMPORAL_TYPES = ("DATE", "TIME", "TIMESTAMP", "TIMESTAMP_S", "TIMESTAMP_MS",
                   "TIMESTAMP_NS", "TIMESTAMP WITH TIME ZONE")


def kind_of(declared_type: str) -> str:
    t = declared_type.upper()
    if t.startswith(_NUMERIC_TYPES):
        return "numeric"
    if t.startswith(_TEMPORAL_TYPES):
        return "temporal"
    if t.startswith("BOOLEAN"):
        return "boolean"
    return "text"


def q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass
class ColumnProfile:
    name: str
    declared_type: str
    kind: str
    rows: int
    non_null: int
    distinct: int
    distinct_exact: bool
    quantiles: dict[str, float] = field(default_factory=dict)
    mean: float | None = None
    stddev: float | None = None
    robust_sigma: float | None = None
    sigma_ratio: float | None = None
    zeros: int | None = None
    negatives: int | None = None
    integral: bool | None = None
    min_value: Any = None
    max_value: Any = None
    min_length: int | None = None
    max_length: int | None = None
    top_values: list[tuple[Any, int]] = field(default_factory=list)
    sentinel_candidates: dict[str, int] = field(default_factory=dict)

    @property
    def nulls(self) -> int:
        return self.rows - self.non_null

    @property
    def null_fraction(self) -> float:
        return (self.nulls / self.rows) if self.rows else 0.0

    def observed_properties(self) -> list[dict]:
        """Properties the measurements *suggest*, each carrying its evidence.

        These are candidates, never confirmations. They are shown to the user so
        a wrong one is visible and correctable rather than silently governing a
        check -- cheaper than asking, and safer than assuming in the dark.
        """
        out: list[dict] = []

        def add(name: str, holds: bool, evidence: str) -> None:
            out.append({"property": name, "holds": holds, "evidence": evidence})

        if self.non_null == 0:
            add("all_null", True, f"0 of {self.rows} rows carry a value")
            return out
        if self.distinct <= 1:
            add("constant", True, f"1 distinct value across {self.non_null} rows")
        if self.distinct == self.non_null == self.rows:
            add("candidate_key", True, f"{self.distinct} distinct values, no nulls")
        if self.null_fraction > 0.2:
            add("high_null", True, f"{self.null_fraction:.1%} null")

        if self.kind == "numeric":
            if self.min_value is not None:
                add("non_negative", self.min_value >= 0, f"min = {self.min_value}")
            if self.integral is not None:
                add("integral", self.integral,
                    "every value equals its floor" if self.integral
                    else "fractional values present")
            if self.min_value is not None and self.max_value is not None:
                if self.min_value >= 0 and self.max_value <= 1:
                    add("unit_interval", True, f"range [{self.min_value}, {self.max_value}]")
                elif self.min_value >= 0 and self.max_value <= 100:
                    named = bool(PERCENTISH_NAME.search(self.name))
                    add("percent_range", True,
                        f"range [{self.min_value}, {self.max_value}] — "
                        "consistent with a percentage"
                        + (", and the name says so" if named
                           else "; nothing but the range suggests it"))

            # The gate the whole outlier family hangs on.
            if self.non_null < MIN_N_FOR_SIGMA:
                add("sigma_establishable", False,
                    f"only {self.non_null} values (need {MIN_N_FOR_SIGMA})")
            elif self.sigma_ratio is None:
                add("sigma_establishable", False, "no dispersion: IQR is zero")
            elif self.sigma_ratio > HEAVY_TAIL_RATIO:
                add("sigma_establishable", False,
                    f"stddev is {self.sigma_ratio:.1f}x the robust scale "
                    f"(IQR/1.349) — tail-inflated, use a robust rule")
            else:
                add("sigma_establishable", True,
                    f"stddev within {self.sigma_ratio:.2f}x the robust scale")

        for value, count in self.sentinel_candidates.items():
            add("numeric_sentinel", True,
                f"{value} occurs {count}x and sits at the extreme of the range")
        return out

    def to_dict(self) -> dict:
        """The machine contract -- and the exact payload an LLM would receive.

        Lexical min/max on a text column are RAW CELL VALUES. On a
        high-cardinality column that is a customer name or an account number, so
        they are withheld here even though they are measured and shown locally.
        Length extremes carry the signal that mattered anyway.

        Low-cardinality top-k values do survive, because characterizing a
        categorical column without seeing its categories is not possible. That
        is a deliberate exception, capped at TOPK_MAX_CARDINALITY distinct
        values, and it is flagged in the payload so it is never a surprise.
        """
        lexical = self.kind == "text"
        d = {
            "name": self.name,
            "declared_type": self.declared_type,
            "kind": self.kind,
            "rows": self.rows,
            "non_null": self.non_null,
            "nulls": self.nulls,
            "null_fraction": round(self.null_fraction, 6),
            "distinct": self.distinct,
            "distinct_exact": self.distinct_exact,
            "min": None if lexical else _jsonable(self.min_value),
            "max": None if lexical else _jsonable(self.max_value),
            "observed_properties": self.observed_properties(),
        }
        if lexical:
            d["lexical_extremes_withheld"] = True
        if self.kind == "numeric":
            d |= {
                "mean": self.mean,
                "stddev": self.stddev,
                "robust_sigma": self.robust_sigma,
                "sigma_ratio": self.sigma_ratio,
                "zeros": self.zeros,
                "negatives": self.negatives,
                "integral": self.integral,
                "quantiles": self.quantiles,
                "sentinel_candidates": self.sentinel_candidates,
            }
        if self.kind == "text":
            d |= {"min_length": self.min_length, "max_length": self.max_length}
        if self.top_values:
            d["top_values"] = [[_jsonable(v), c] for v, c in self.top_values]
            d["top_values_are_raw_values"] = True
        return d


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _aggregate_sql(name: str, kind: str, exact_distinct: bool) -> list[tuple[str, str]]:
    """(alias_suffix, expression) pairs for one column."""
    c = q(name)
    distinct = f"count(DISTINCT {c})" if exact_distinct else f"approx_count_distinct({c})"
    parts = [
        ("non_null", f"count({c})"),
        ("distinct", distinct),
        ("min", f"min({c})"),
        ("max", f"max({c})"),
    ]
    if kind == "numeric":
        parts += [
            ("mean", f"avg({c})"),
            ("stddev", f"stddev_samp({c})"),
            ("quantiles", f"quantile_cont({c}, {QUANTILES})"),
            ("zeros", f"count(*) FILTER (WHERE {c} = 0)"),
            ("negatives", f"count(*) FILTER (WHERE {c} < 0)"),
            ("integral", f"bool_and({c} = floor({c}))"),
        ]
        for i, s in enumerate(NUMERIC_SENTINELS):
            parts.append((f"sent{i}", f"count(*) FILTER (WHERE {c} = {s})"))
    elif kind == "text":
        parts += [
            ("minlen", f"min(length({c}))"),
            ("maxlen", f"max(length({c}))"),
        ]
    return parts


def profile_columns(
    con: duckdb.DuckDBPyConnection,
    source: str,
    params: list[str],
    schema: list[tuple[str, str]],
    row_count: int,
) -> list[ColumnProfile]:
    """One aggregate pass over `source`, plus one GROUP BY per top-k column."""
    exact = row_count <= EXACT_DISTINCT_LIMIT
    kinds = {name: kind_of(dtype) for name, dtype in schema}

    selects, index = [], {}
    for pos, (name, _) in enumerate(schema):
        for suffix, expr in _aggregate_sql(name, kinds[name], exact):
            alias = f"c{pos}_{suffix}"
            index[(pos, suffix)] = alias
            selects.append(f"{expr} AS {alias}")

    if not selects:
        return []

    # Fetched positionally and zipped against cursor.description -- fetchdf()
    # would drag pandas in, and the one-dependency promise is load-bearing.
    cursor = con.execute(f"SELECT {', '.join(selects)} FROM {source}", params)
    values = cursor.fetchone()
    order = [d[0] for d in cursor.description]
    row = dict(zip(order, values, strict=True))

    profiles = []
    for pos, (name, dtype) in enumerate(schema):
        kind = kinds[name]

        def g(suffix: str, _pos: int = pos) -> Any:
            return row.get(index.get((_pos, suffix), ""))

        p = ColumnProfile(
            name=name,
            declared_type=dtype,
            kind=kind,
            rows=row_count,
            non_null=int(g("non_null") or 0),
            distinct=int(g("distinct") or 0),
            distinct_exact=exact,
            min_value=g("min"),
            max_value=g("max"),
        )

        if kind == "numeric":
            p.mean = _f(g("mean"))
            p.stddev = _f(g("stddev"))
            qs = g("quantiles") or []
            # An all-null column yields NULL rather than a list; leave the
            # quantile map empty rather than inventing entries for it.
            if len(qs) == len(QUANTILES):
                p.quantiles = {
                    str(k): _f(v) for k, v in zip(QUANTILES, qs, strict=True)
                }
            p.zeros = int(g("zeros") or 0)
            p.negatives = int(g("negatives") or 0)
            p.integral = g("integral")
            iqr_hi, iqr_lo = p.quantiles.get("0.75"), p.quantiles.get("0.25")
            if iqr_hi is not None and iqr_lo is not None:
                robust = (iqr_hi - iqr_lo) / 1.349
                p.robust_sigma = round(robust, 6)
                if robust > 0 and p.stddev is not None:
                    p.sigma_ratio = round(p.stddev / robust, 4)
            for i, s in enumerate(NUMERIC_SENTINELS):
                count = int(g(f"sent{i}") or 0)
                # Only a candidate if it sits at an extreme; -99 in the middle of
                # a distribution is far more likely to be a real observation.
                if count and (s == _f(p.min_value) or s == _f(p.max_value)):
                    p.sentinel_candidates[str(s)] = count
        elif kind == "text":
            p.min_length = _int_or_none(g("minlen"))
            p.max_length = _int_or_none(g("maxlen"))

        # Top-k is only meaningful where the value set is small enough to
        # be read; on a high-cardinality column it costs a pass and says
        # nothing.
        if 0 < p.distinct <= TOPK_MAX_CARDINALITY:
            p.top_values = con.execute(
                f"SELECT {q(name)}, count(*) c FROM {source} "
                f"WHERE {q(name)} IS NOT NULL GROUP BY 1 ORDER BY c DESC, 1 "
                f"LIMIT {TOPK_LIMIT}",
                params,
            ).fetchall()

        profiles.append(p)
    return profiles


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(v: Any) -> int | None:
    return int(v) if v is not None else None
