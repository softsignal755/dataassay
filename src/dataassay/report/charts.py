"""Inline SVG charts. No library, no CDN, no dependency.

The vocabulary is deliberately tiny. A chart here has one job: let a reader
confirm or reject ONE finding at a glance. That rules out a gallery of general
plots and leaves four forms:

  series      a line through time with the flagged points marked
  decades     counts by order of magnitude -- the form that makes a residue
              gap visible, because the gap IS the finding
  histogram   value distribution with the bound that values pile up on marked
  timeline    periods present vs. absent

Colour follows the roles in the design system: one series hue for data, a
second for what is wrong with it. Status colour NEVER carries meaning alone --
every flagged thing also gets a rule and a direct label, and every two-category
chart gets a legend. That is not decoration: the warning step is deliberately
sub-3:1 on a light surface, so a reader who cannot separate the hues has to be
able to read the chart anyway.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

W, H = 720, 210
PAD_L, PAD_R, PAD_B = 52, 16, 30
# Two top paddings: a bare chart starts high, one carrying a legend reserves a
# band for it. Sharing one value put the legend, the top axis tick and the
# annotation label in the same 12 pixels.
PAD_T = 18
PAD_T_LEGEND = 40
PLOT_W = W - PAD_L - PAD_R

# Roles, not hex, everywhere below. The page defines both modes.
SERIES = "var(--series-1)"
CRITICAL = "var(--status-critical)"
WARNING = "var(--status-warning)"
SURFACE = "var(--surface-1)"
GRID = "var(--grid)"
AXIS = "var(--baseline)"
MUTED = "var(--text-muted)"


def esc(s) -> str:
    return html.escape(str(s), quote=True)


@dataclass
class Scale:
    lo: float
    hi: float
    px0: float
    px1: float

    def __call__(self, v: float) -> float:
        if self.hi == self.lo:
            return (self.px0 + self.px1) / 2
        t = (v - self.lo) / (self.hi - self.lo)
        return self.px0 + t * (self.px1 - self.px0)


def _nice(lo: float, hi: float) -> tuple[float, float]:
    if hi == lo:
        pad = abs(hi) * 0.1 or 1.0
        return lo - pad, hi + pad
    pad = (hi - lo) * 0.08
    return lo - pad, hi + pad


def _fmt(v: float) -> str:
    a = abs(v)
    if a and (a < 1e-3 or a >= 1e7):
        return f"{v:.1e}"
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 1:
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{v:.4g}"


def _frame(body: str, label: str, ylab: str = "", top: float = PAD_T) -> str:
    return (
        f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="{esc(label)}" preserveAspectRatio="xMidYMid meet">'
        f'<title>{esc(label)}</title>'
        + (f'<text x="4" y="{top - 6:.0f}" class="axlabel">{esc(ylab)}</text>'
           if ylab else "")
        + body
        + "</svg>"
    )


def _annotate(x: float, y: float, text: str) -> str:
    """A label pinned inside the plot, anchored away from whichever edge it is
    nearest so it never runs off."""
    anchor = "end" if x > W - PAD_R - 90 else "start"
    dx = -6 if anchor == "end" else 6
    x = min(max(x + dx, PAD_L + 2), W - PAD_R - 2)
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" class="tick" text-anchor="{anchor}" '
        f'style="font-weight:600">{esc(text)}</text>'
    )


def _gridlines(ys: Scale, lo: float, hi: float) -> str:
    out = []
    for i in range(4):
        v = lo + (hi - lo) * i / 3
        y = ys(v)
        out.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
            f'<text x="{PAD_L - 6}" y="{y + 3.5:.1f}" class="tick" '
            f'text-anchor="end">{esc(_fmt(v))}</text>'
        )
    return "".join(out)


def _legend(items) -> str:
    """items: [(colour, label)]. Required wherever two categories share a chart."""
    out, x = [], PAD_L
    for colour, label in items:
        out.append(
            f'<rect x="{x}" y="4" width="9" height="9" rx="2" fill="{colour}"/>'
            f'<text x="{x + 13}" y="12.5" class="tick">{esc(label)}</text>'
        )
        x += 22 + len(label) * 6.2
    return "".join(out)


def series(points, flagged=None, band=None, label="series", ylab="") -> str:
    """points: [(x_label, y)] in order.

    flagged: {index: (status, note)} for point events — each gets a rule and one
    direct label, never a bare coloured dot.
    band: (start, end, status, note) for a flagged RANGE — drawn as a region
    with a single label, because a marker on every point in a run is exactly the
    "number on every point" anti-pattern.
    """
    if len(points) < 2:
        return ""
    flagged = flagged or {}
    ys_vals = [p[1] for p in points]
    lo, hi = _nice(min(ys_vals), max(ys_vals))
    xs = Scale(0, len(points) - 1, PAD_L, W - PAD_R)
    ysc = Scale(lo, hi, H - PAD_B, PAD_T)
    top = PAD_T + 11

    body = [_gridlines(ysc, lo, hi)]

    if band:
        b0, b1, status, note = band
        colour = CRITICAL if status == "critical" else WARNING
        x0, x1 = xs(b0), xs(b1)
        body.append(
            f'<rect x="{x0:.1f}" y="{PAD_T}" width="{max(x1 - x0, 2):.1f}" '
            f'height="{H - PAD_B - PAD_T}" fill="{colour}" opacity="0.14"/>'
            f'<line x1="{x0:.1f}" y1="{PAD_T}" x2="{x0:.1f}" y2="{H - PAD_B}" '
            f'stroke="{colour}" stroke-width="2"/>'
        )
        body.append(_annotate(x0, top, note))

    path = " ".join(
        f"{'M' if i == 0 else 'L'}{xs(i):.1f},{ysc(v):.1f}"
        for i, (_, v) in enumerate(points)
    )
    body.append(
        f'<path d="{path}" fill="none" stroke="{SERIES}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
    )

    for i, (xlab, v) in enumerate(points):
        if i not in flagged:
            continue
        status, note = flagged[i]
        colour = CRITICAL if status == "critical" else WARNING
        x, y = xs(i), ysc(v)
        # Rule + label + marker: three channels, so hue is never the only one.
        body.append(
            f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{H - PAD_B}" '
            f'stroke="{colour}" stroke-width="1" stroke-dasharray="3 3"/>'
        )
        body.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{colour}" '
            f'stroke="{SURFACE}" stroke-width="2">'
            f'<title>{esc(xlab)}: {esc(_fmt(v))} — {esc(note)}</title></circle>'
        )
        body.append(_annotate(x, top, note))

    marks = {0, len(points) - 1}
    for i in sorted(marks):
        anchor = "start" if i == 0 else "end"
        body.append(
            f'<text x="{xs(i):.1f}" y="{H - 10}" class="tick" '
            f'text-anchor="{anchor}">{esc(points[i][0])}</text>'
        )
    for i in sorted(flagged):
        body.append(
            f'<text x="{xs(i):.1f}" y="{H - 10}" class="tick" '
            f'text-anchor="middle">{esc(points[i][0])}</text>'
        )
    body.append(
        f'<line x1="{PAD_L}" y1="{H - PAD_B}" x2="{W - PAD_R}" y2="{H - PAD_B}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )
    return _frame("".join(body), label, ylab)


def decades(rows, cluster_max_decade, label="magnitudes") -> str:
    """rows: [(decade, count)] sorted. Bars for every decade in the span, so the
    EMPTY ones between residue and data are visible -- the gap is the finding,
    and a chart that omits empty categories hides it."""
    if not rows:
        return ""
    lo_d, hi_d = rows[0][0], rows[-1][0]
    span = list(range(int(lo_d), int(hi_d) + 1))
    counts = dict(rows)
    hi = max(counts.values())
    xs = Scale(0, max(len(span) - 1, 1), PAD_L + 8, W - PAD_R - 8)
    ysc = Scale(0, hi, H - PAD_B, PAD_T_LEGEND)
    bw = max(3.0, min(26.0, (PLOT_W - 16) / max(len(span), 1) - 2))

    body = [_gridlines(ysc, 0, hi)]
    for i, d in enumerate(span):
        n = counts.get(d, 0)
        x = xs(i) - bw / 2
        colour = CRITICAL if d <= cluster_max_decade else SERIES
        if n:
            h = (H - PAD_B) - ysc(n)
            body.append(
                f'<rect x="{x:.1f}" y="{ysc(n):.1f}" width="{bw:.1f}" '
                f'height="{max(h, 2):.1f}" rx="3" fill="{colour}">'
                f'<title>1e{d}: {n:,} value(s)</title></rect>'
            )
        else:
            body.append(
                f'<rect x="{x:.1f}" y="{H - PAD_B - 2}" width="{bw:.1f}" '
                f'height="2" rx="1" fill="{GRID}">'
                f'<title>1e{d}: nothing</title></rect>'
            )
    for i in (0, len(span) - 1):
        body.append(
            f'<text x="{xs(i):.1f}" y="{H - 10}" class="tick" '
            f'text-anchor="middle">1e{span[i]}</text>'
        )
    residue = [i for i, d in enumerate(span) if d <= cluster_max_decade and counts.get(d)]
    if residue:
        body.append(_annotate(xs(max(residue)), PAD_T_LEGEND - 8, "should be zero"))
    body.append(
        f'<line x1="{PAD_L}" y1="{H - PAD_B}" x2="{W - PAD_R}" y2="{H - PAD_B}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )
    body.insert(0, _legend([(CRITICAL, "residue"), (SERIES, "measured"),
                            (GRID, "no values")]))
    return _frame("".join(body), label, "count", top=PAD_T_LEGEND)


def histogram(bins, marked_index=None, label="distribution") -> str:
    """bins: [(left_edge, count)]."""
    if not bins:
        return ""
    hi = max(c for _, c in bins) or 1
    xs = Scale(0, max(len(bins) - 1, 1), PAD_L + 8, W - PAD_R - 8)
    ysc = Scale(0, hi, H - PAD_B, PAD_T)
    bw = max(2.0, (PLOT_W - 16) / max(len(bins), 1) - 2)

    body = [_gridlines(ysc, 0, hi)]
    for i, (edge, n) in enumerate(bins):
        if not n:
            continue
        colour = CRITICAL if i == marked_index else SERIES
        h = (H - PAD_B) - ysc(n)
        body.append(
            f'<rect x="{xs(i) - bw / 2:.1f}" y="{ysc(n):.1f}" width="{bw:.1f}" '
            f'height="{max(h, 2):.1f}" rx="3" fill="{colour}">'
            f'<title>{esc(_fmt(edge))}: {n:,}</title></rect>'
        )
    if marked_index is not None and bins[marked_index][1]:
        body.append(_annotate(
            xs(marked_index), PAD_T + 11,
            f"pile-up at {_fmt(bins[marked_index][0])}",
        ))
    for i in (0, len(bins) - 1):
        body.append(
            f'<text x="{xs(i):.1f}" y="{H - 10}" class="tick" '
            f'text-anchor="middle">{esc(_fmt(bins[i][0]))}</text>'
        )
    body.append(
        f'<line x1="{PAD_L}" y1="{H - PAD_B}" x2="{W - PAD_R}" y2="{H - PAD_B}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )
    return _frame("".join(body), label, "count")


def timeline(cells, label="coverage") -> str:
    """cells: [(label, present: bool)] in period order."""
    if not cells:
        return ""
    h = 56
    top = 30
    cw = max(1.5, (PLOT_W) / max(len(cells), 1))
    gap = 1.0 if cw > 4 else 0.0
    body = []
    for i, (lab, present) in enumerate(cells):
        x = PAD_L + i * cw
        colour = SERIES if present else CRITICAL
        body.append(
            f'<rect x="{x:.2f}" y="{top}" width="{max(cw - gap, 1):.2f}" '
            f'height="{h}" rx="1.5" fill="{colour}">'
            f'<title>{esc(lab)}: {"present" if present else "MISSING"}</title></rect>'
        )
    body.insert(0, _legend([(SERIES, "period present"), (CRITICAL, "missing")]))
    body.append(
        f'<text x="{PAD_L}" y="{top + h + 18}" class="tick" '
        f'text-anchor="start">{esc(cells[0][0])}</text>'
    )
    body.append(
        f'<text x="{W - PAD_R}" y="{top + h + 18}" class="tick" '
        f'text-anchor="end">{esc(cells[-1][0])}</text>'
    )
    return (
        f'<svg class="chart" viewBox="0 0 {W} {h + top + 26}" role="img" '
        f'aria-label="{esc(label)}" preserveAspectRatio="xMidYMid meet">'
        f'<title>{esc(label)}</title>{"".join(body)}</svg>'
    )
