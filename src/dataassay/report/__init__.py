"""Build the report: one HTML file, plus the flagged list as CSV."""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb

from dataassay.checks.base import CheckContext
from dataassay.report import charts, evidence
from dataassay.report.html import render

FLAGGED_SUFFIX = ".flagged.csv"


def _chart_for(f, ctx) -> str:
    """One chart per finding, or none. A chart's job is to let this ONE claim be
    confirmed or rejected at a glance; a finding a picture cannot settle gets
    text instead of decoration."""
    col, ev = f.column, f.evidence
    try:
        if f.check_id == "level_shift" and col:
            at = ev.get("at")
            group = evidence.locate_series(ctx, col, at)
            points, idx = evidence.series_around(ctx, col, at, group)
            if not points or idx is None:
                return ""
            status = "critical" if f.disposition == "defect" else "warning"
            note = "level holds" if ev.get("persists") else "reverts"
            where = f" ({', '.join(str(g) for g in group)})" if group else ""
            return charts.series(
                points, {idx: (status, note)},
                label=f"{col}{where} around {at}", ylab=col,
            )

        if f.check_id == "flatline_tail" and col:
            run = int(ev.get("run_length") or 0)
            points = evidence.series_tail(ctx, col, run + 16)
            if len(points) < 3:
                return ""
            start = max(len(points) - run, 0)
            return charts.series(
                points,
                band=(start, len(points) - 1, "warning",
                      f"identical for {run} observations"),
                label=f"{col}, most recent", ylab=col,
            )

        if f.check_id == "negligible_residue" and col:
            rows = evidence.decade_rows(ctx, col)
            top = ev.get("next_decade_above")
            cluster_max = (top - ev.get("gap_decades", 0)) if top is not None else -99
            return charts.decades(
                rows, cluster_max, label=f"{col}: values by order of magnitude"
            )

        if f.check_id == "saturation_at_bound" and col:
            bins, marked = evidence.value_bins(ctx, col, float(ev.get("bound", 0)))
            if not bins:
                return ""
            return charts.histogram(bins, marked, label=f"{col}: distribution")

        if f.check_id == "cadence_gap":
            largest = ev.get("largest") or [None]
            series_key = ev.get("series") or None
            cells = evidence.coverage_cells(
                ctx, int(ev.get("cadence_days") or 1),
                group=series_key, focus=largest[0],
            )
            if len(cells) < 3:
                return ""
            windowed = "" if len(cells) < evidence.MAX_TIMELINE_CELLS else " (window)"
            where = f" — {' / '.join(series_key)}" if series_key else ""
            return charts.timeline(
                cells, label=f"periods present and missing{where}{windowed}"
            )
    except (duckdb.Error, ValueError, TypeError, KeyError):
        # A chart is an aid, never the finding. If the data behind one cannot be
        # fetched, the finding still stands on its evidence and predicate.
        return ""
    return ""


def _flagged_rows(audit) -> list[dict]:
    out = []
    for f in audit.findings:
        ev = f.evidence
        where = (
            ev.get("at")
            or ev.get("from")
            or (ev.get("largest") or [None])[0]
            or ""
        )
        out.append({
            "check": f.check_id,
            "disposition": f.disposition,
            "confidence": f.confidence.level,
            "column": f.column or "",
            "where": where,
            "rows_affected": ev.get("rows") or ev.get("excess_rows") or "",
            "summary": f.summary,
            "predicate": f.predicate,
        })
    return out


def write_flagged(audit, path: Path) -> Path:
    rows = _flagged_rows(audit)
    fields = ["check", "disposition", "confidence", "column", "where",
              "rows_affected", "summary", "predicate"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


def build(audit, data_file: Path, out_html: Path) -> tuple[Path, Path]:
    """Render the report and the flagged list. Returns (html, csv)."""
    from dataassay.provenance import reader_for, source_expr

    reader = reader_for(data_file)
    con = duckdb.connect(":memory:")
    try:
        ctx = CheckContext(
            profile=audit.profile,
            structure=audit.structure,
            con=con,
            source=source_expr(reader, audit.profile.provenance.read_mode),
            params=[str(data_file)],
        )
        chart_map = {id(f): _chart_for(f, ctx) for f in audit.findings}
    finally:
        con.close()

    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(render(audit, chart_map), encoding="utf-8")
    csv_path = out_html.with_suffix("")
    csv_path = csv_path.with_name(csv_path.name + FLAGGED_SUFFIX)
    write_flagged(audit, csv_path)
    return out_html, csv_path
