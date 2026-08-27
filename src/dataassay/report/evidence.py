"""Fetches exactly the numbers each chart needs, at report time.

Deliberately not carried in the findings JSON. That payload is the machine
contract and the thing an LLM would be shown; padding it with a few hundred
plot points per finding would bloat it and quietly put data rows into a
metadata-only envelope.
"""

from __future__ import annotations

from dataassay.columns import q

MAX_TIMELINE_CELLS = 420
CONTEXT_SPAN = 24


def _where_group(structure, values) -> str:
    if not values:
        return ""
    parts = [
        f"{q(c)} = '{str(v).replace(chr(39), chr(39) * 2)}'"
        for c, v in zip(structure.group_columns, values, strict=True)
    ]
    return " AND " + " AND ".join(parts)


def locate_series(ctx, column: str, at: str):
    """Which series does a flagged timestamp belong to?

    On a panel, plotting every row in date order interleaves the series into
    nonsense. The one that matters is the series whose value at that instant is
    furthest from its own neighbours, so that is the one asked for.
    """
    s = ctx.structure
    if not s.group_columns:
        return None
    cols = ", ".join(q(c) for c in s.group_columns)
    rows = ctx.fetch(
        f"SELECT {cols} FROM {ctx.source} "
        f"WHERE CAST({q(s.time_axis)} AS VARCHAR) = '{at}' "
        f"  AND {q(column)} IS NOT NULL LIMIT 1"
    )
    return rows[0] if rows else None


def series_around(ctx, column: str, at: str, group=None):
    """(points, flagged_index) for the window either side of a timestamp."""
    s = ctx.structure
    axis, gwhere = q(s.time_axis), _where_group(s, group)
    rows = ctx.fetch(
        f"WITH s AS (SELECT {axis} AS t, {q(column)} AS v FROM {ctx.source} "
        f"           WHERE {q(column)} IS NOT NULL{gwhere}), "
        f"n AS (SELECT *, row_number() OVER (ORDER BY t) AS rn FROM s), "
        f"hit AS (SELECT rn FROM n WHERE CAST(t AS VARCHAR) = '{at}' LIMIT 1) "
        f"SELECT n.t, n.v FROM n, hit "
        f"WHERE n.rn BETWEEN hit.rn - {CONTEXT_SPAN} AND hit.rn + {CONTEXT_SPAN} "
        f"ORDER BY n.t"
    )
    points = [(str(t), float(v)) for t, v in rows]
    idx = next((i for i, (t, _) in enumerate(points) if t == at), None)
    return points, idx


def series_tail(ctx, column: str, n: int, group=None):
    s = ctx.structure
    axis, gwhere = q(s.time_axis), _where_group(s, group)
    rows = ctx.fetch(
        f"SELECT t, v FROM (SELECT {axis} AS t, {q(column)} AS v FROM {ctx.source} "
        f"  WHERE {q(column)} IS NOT NULL{gwhere} ORDER BY t DESC LIMIT {n}) "
        f"ORDER BY t"
    )
    return [(str(t), float(v)) for t, v in rows]


def decade_rows(ctx, column: str):
    return [
        (int(d), int(n))
        for d, n in ctx.fetch(
            f"SELECT floor(log10(abs({q(column)})))::BIGINT AS d, count(*) "
            f"FROM {ctx.source} WHERE {q(column)} IS NOT NULL "
            f"  AND {q(column)} <> 0 AND isfinite({q(column)}) "
            f"GROUP BY 1 ORDER BY 1"
        )
    ]


def value_bins(ctx, column: str, bound: float, nbins: int = 40):
    """Equal-width bins, plus which bin the pile-up sits in."""
    rows = ctx.fetch(
        f"SELECT min({q(column)}), max({q(column)}) FROM {ctx.source} "
        f"WHERE {q(column)} IS NOT NULL AND isfinite({q(column)})"
    )
    lo, hi = rows[0]
    if lo is None or hi is None or hi == lo:
        return [], None
    lo, hi = float(lo), float(hi)
    width = (hi - lo) / nbins
    counts = dict(
        ctx.fetch(
            f"SELECT least(floor(({q(column)} - {lo}) / {width})::BIGINT, "
            f"       {nbins - 1}) AS b, count(*) FROM {ctx.source} "
            f"WHERE {q(column)} IS NOT NULL AND isfinite({q(column)}) GROUP BY 1"
        )
    )
    bins = [(lo + i * width, int(counts.get(i, 0))) for i in range(nbins)]
    marked = min(int((bound - lo) / width) if width else 0, nbins - 1)
    return bins, max(marked, 0)


def coverage_cells(ctx, cadence_days: int, group=None):
    """One cell per expected period between the first and last observation."""
    s = ctx.structure
    axis, gwhere = _where_group(s, group), None
    del gwhere
    rows = ctx.fetch(
        f"SELECT min(CAST({q(s.time_axis)} AS DATE)), "
        f"       max(CAST({q(s.time_axis)} AS DATE)) FROM {ctx.source}"
        + (f" WHERE 1=1{axis}" if axis else "")
    )
    lo, hi = rows[0]
    if lo is None or hi is None:
        return []
    present = {
        r[0]
        for r in ctx.fetch(
            f"SELECT DISTINCT CAST({q(s.time_axis)} AS DATE) FROM {ctx.source}"
        )
    }
    from datetime import timedelta

    cells, cur = [], lo
    while cur <= hi:
        cells.append((str(cur), cur in present))
        cur = cur + timedelta(days=cadence_days)
    if len(cells) > MAX_TIMELINE_CELLS:
        cells = cells[-MAX_TIMELINE_CELLS:]
    return cells
