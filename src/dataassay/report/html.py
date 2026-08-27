"""The report: one self-contained file, no network, no dependency.

Ordered the way the design argues it should be. Provenance first, because a
finding that cannot be tied to exact bytes is worthless once the file is
regenerated. Coverage BEFORE the findings, because an empty findings list is
uninterpretable without knowing how many checks ran -- zero findings at 30%
coverage and zero at 95% are different objects. Then the findings, each with
its governing property, one chart, the composition of its confidence, and the
predicate that produced it. Then characterization, where a reader can catch US
being wrong. Then every check, including the ones that passed.

The findings JSON is embedded in the page rather than written beside it, so the
human artifact and the machine contract are one file and cannot drift.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from dataassay.checks.base import BOOKKEEPING, DEFECT, SUSPECT
from dataassay.checks.registry import CATALOG
from dataassay.profile import LIMITATION, OBSERVATION, QUESTION
from dataassay.report.charts import esc

_DISPOSITION = {
    DEFECT: ("Likely defect", "critical", "▲"),
    SUSPECT: ("Worth a look", "warning", "◆"),
    BOOKKEEPING: ("Source bookkeeping", "neutral", "■"),
}

_NOTE_LABEL = {
    QUESTION: ("Needs an answer", "warning", "?"),
    LIMITATION: ("Could not be checked", "neutral", "—"),
    OBSERVATION: ("Noted", "neutral", "·"),
}

CSS = """
:root {
  color-scheme: light;
  --surface-page: #f9f9f7;
  --surface-1:    #fcfcfb;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted:   #898781;
  --grid:         #e1e0d9;
  --baseline:     #c3c2b7;
  --border:       rgba(11,11,11,0.10);
  --series-1:     #2a78d6;
  --status-good:     #0ca30c;
  --status-warning:  #fab219;
  --status-serious:  #ec835a;
  --status-critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface-page: #0d0d0d;
    --surface-1:    #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:   #898781;
    --grid:         #2c2c2a;
    --baseline:     #383835;
    --border:       rgba(255,255,255,0.10);
    --series-1:     #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-page: #0d0d0d;
  --surface-1:    #1a1a19;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted:   #898781;
  --grid:         #2c2c2a;
  --baseline:     #383835;
  --border:       rgba(255,255,255,0.10);
  --series-1:     #3987e5;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 5rem;
  background: var(--surface-page); color: var(--text-primary);
  font: 15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 880px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: -0.01em; }
h2 { font-size: 1.05rem; margin: 2.5rem 0 .75rem; letter-spacing: -0.005em; }
h2 .count { color: var(--text-muted); font-weight: 400; }
h3 { font-size: .95rem; margin: 0 0 .35rem; }
p  { margin: .4rem 0; }
.sub { color: var(--text-secondary); margin: 0 0 1.5rem; }
.card {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 1rem 1.15rem; margin: .75rem 0;
}
.meta { display: grid; grid-template-columns: repeat(auto-fit,minmax(190px,1fr));
        gap: .5rem 1.5rem; }
.meta div { min-width: 0; }
.meta dt { color: var(--text-muted); font-size: .75rem; text-transform: uppercase;
           letter-spacing: .06em; }
.meta dd { margin: 0; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.badge {
  display: inline-flex; align-items: center; gap: .35rem;
  font-size: .75rem; font-weight: 600; padding: .12rem .5rem;
  border-radius: 999px; border: 1px solid var(--border);
  color: var(--text-primary); white-space: nowrap;
}
.badge .dot { width: .6rem; height: .6rem; border-radius: 50%; }
.badge.critical .dot { background: var(--status-critical); }
.badge.warning  .dot { background: var(--status-warning); }
.badge.neutral  .dot { background: var(--text-muted); }
.badge.good     .dot { background: var(--status-good); }
.finding-head { display: flex; gap: .6rem; align-items: baseline;
                flex-wrap: wrap; margin-bottom: .35rem; }
.col { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
       font-size: .85rem; color: var(--text-secondary); }
.why { color: var(--text-secondary); font-size: .875rem; margin-top: .5rem; }
.why ul { margin: .25rem 0 0; padding-left: 1.1rem; }
.chart { width: 100%; height: auto; display: block; margin: .9rem 0 .35rem; }
.tick { fill: var(--text-muted); font-size: 11px;
        font-family: system-ui, sans-serif; font-variant-numeric: tabular-nums; }
.axlabel { fill: var(--text-muted); font-size: 10px; text-transform: uppercase;
           letter-spacing: .06em; font-family: system-ui, sans-serif; }
pre.pred {
  background: var(--surface-page); border: 1px solid var(--border);
  border-radius: 6px; padding: .55rem .7rem; margin: .6rem 0 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .78rem; color: var(--text-secondary);
  overflow-x: auto; white-space: pre;
}
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid var(--grid);
         white-space: nowrap; }
th { color: var(--text-muted); font-weight: 600; font-size: .72rem;
     text-transform: uppercase; letter-spacing: .05em; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.empty { color: var(--text-secondary); }
details { margin-top: .5rem; }
summary { cursor: pointer; color: var(--text-secondary); font-size: .875rem; }
.foot { color: var(--text-muted); font-size: .8rem; margin-top: 3rem;
        border-top: 1px solid var(--border); padding-top: 1rem; }
"""


def _badge(label: str, tone: str, glyph: str) -> str:
    return (
        f'<span class="badge {tone}"><span class="dot"></span>'
        f'{esc(glyph)} {esc(label)}</span>'
    )


def _finding_card(f, chart: str) -> str:
    label, tone, glyph = _DISPOSITION.get(f.disposition, ("Finding", "neutral", "·"))
    head = [_badge(label, tone, glyph), _badge(f.confidence.level, "neutral", "◇")]
    if f.column:
        head.append(f'<span class="col">{esc(f.column)}</span>')
    why = "".join(f"<li>{esc(i)}</li>" for i in f.confidence.inputs)
    return (
        '<div class="card">'
        f'<div class="finding-head">{"".join(head)}</div>'
        f"<p>{esc(f.summary)}</p>"
        f"{chart}"
        f'<div class="why">Why this reading:<ul>{why}</ul></div>'
        f'<details><summary>Reproduce this without our code</summary>'
        f'<pre class="pred">{esc(f.predicate)}</pre></details>'
        "</div>"
    )


def _coverage(audit) -> str:
    c = audit.coverage
    rows = []
    for cid in c.ran:
        rows.append(f"<tr><td>{esc(cid)}</td><td>ran</td><td></td></tr>")
    for cid, reason in c.withheld:
        rows.append(
            f"<tr><td>{esc(cid)}</td><td>withheld</td>"
            f'<td style="white-space:normal">{esc(reason)}</td></tr>'
        )
    for cid, question in c.blocked:
        rows.append(
            f"<tr><td>{esc(cid)}</td><td>waiting on an answer</td>"
            f'<td style="white-space:normal">{esc(question)}</td></tr>'
        )
    return (
        '<div class="card">'
        f"<p><strong>{len(c.ran)} of {c.total} checks ran.</strong> "
        f"{len(c.withheld)} withheld, {len(c.blocked)} waiting on an answer. "
        "A check that could not run is reported here rather than left silent — "
        "without it, an empty findings list means nothing.</p>"
        '<div class="scroll"><table><thead><tr><th>Check</th><th>Status</th>'
        f"<th>Reason</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        "</div>"
    )


def _columns_table(audit) -> str:
    rows = []
    for c in audit.profile.columns:
        props = [p["property"] for p in c.observed_properties() if p["holds"]]
        median = c.quantiles.get("0.5") if c.kind == "numeric" else None
        rows.append(
            "<tr>"
            f'<td class="col">{esc(c.name)}</td><td>{esc(c.declared_type)}</td>'
            f'<td class="num">{c.null_fraction:.1%}</td>'
            f'<td class="num">{c.distinct:,}</td>'
            f'<td class="num">{esc("" if median is None else round(median, 4))}</td>'
            f'<td style="white-space:normal">{esc(", ".join(props))}</td>'
            "</tr>"
        )
    return (
        '<div class="card"><p>What each column was taken to be, and on what '
        "evidence. This is where a wrong assumption of ours is visible — "
        "correcting one here changes which checks are valid.</p>"
        '<div class="scroll"><table><thead><tr><th>Column</th><th>Type</th>'
        "<th>Null</th><th>Distinct</th><th>Median</th><th>Established</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>"
    )


def _register() -> str:
    rows = []
    for check in CATALOG:
        s = check.spec
        rows.append(
            "<tr>"
            f'<td class="col">{esc(s.id)}</td>'
            f'<td style="white-space:normal">{esc(s.detects)}</td>'
            f'<td style="white-space:normal">{esc(s.gate)}</td>'
            "</tr>"
        )
    return (
        '<div class="card"><p>Every check this catalog knows about, whether or '
        "not it ran here.</p>"
        '<div class="scroll"><table><thead><tr><th>Check</th><th>Detects</th>'
        f"<th>Gated on</th></tr></thead><tbody>{''.join(rows)}</tbody>"
        "</table></div></div>"
    )


def _notes(audit) -> str:
    if not audit.profile.notes:
        return ""
    out = []
    for severity in (QUESTION, LIMITATION, OBSERVATION):
        group = [n for n in audit.profile.notes if n.severity == severity]
        if not group:
            continue
        label, tone, glyph = _NOTE_LABEL[severity]
        items = "".join(
            f"<li>{('<span class=col>' + esc(n.column) + '</span> ') if n.column else ''}"
            f"{esc(n.message)}</li>"
            for n in group
        )
        out.append(
            f'<div class="card"><div class="finding-head">'
            f"{_badge(label, tone, glyph)}"
            f'<span class="col">{len(group)}</span></div>'
            f'<ul style="margin:.4rem 0 0;padding-left:1.1rem">{items}</ul></div>'
        )
    return "".join(out)


def render(audit, charts: dict[int, str]) -> str:
    p = audit.provenance if hasattr(audit, "provenance") else audit.profile.provenance
    s = audit.structure
    findings = audit.findings
    by_disposition = {
        d: [f for f in findings if f.disposition == d]
        for d in (DEFECT, SUSPECT, BOOKKEEPING)
    }
    defects = len(by_disposition[DEFECT])

    meta = [
        ("File", p.filename),
        ("Rows × columns", f"{p.row_count:,} × {p.column_count}"),
        ("SHA-256", p.content_sha256),
        ("Audited", p.audited_at),
        ("Tool", f"{p.tool} {p.tool_version}"),
        ("Catalog", audit.catalog_version),
    ]
    if s.time_axis:
        meta.append(("Time axis", f"{s.time_axis} ({s.time_axis_basis})"))
    if s.grain:
        meta.append((
            "Grain",
            " × ".join(s.grain)
            + (" — declared" if s.grain_declared else " — inferred")
            + ("" if s.grain_is_unique else ", NOT unique"),
        ))
    if audit.manifest_path:
        meta.append(("Manifest", audit.manifest_path))
    meta_html = "".join(
        f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in meta
    )

    body = [
        '<div class="wrap">',
        f"<h1>{esc(p.filename)}</h1>",
        f'<p class="sub">{len(findings)} finding(s), {defects} likely defect(s), '
        f"{len(audit.coverage.ran)} of {audit.coverage.total} checks run.</p>",
        f'<div class="card"><dl class="meta">{meta_html}</dl></div>',
        "<h2>Coverage</h2>",
        _coverage(audit),
    ]

    body.append(f'<h2>Findings <span class="count">{len(findings)}</span></h2>')
    if not findings:
        body.append(
            '<div class="card empty">Nothing was found by the checks that ran. '
            "Read that alongside the coverage table above — it is a statement "
            "about those checks, not about the data as a whole.</div>"
        )
    for disposition in (DEFECT, SUSPECT, BOOKKEEPING):
        group = by_disposition[disposition]
        if not group:
            continue
        label = _DISPOSITION[disposition][0]
        body.append(f'<h3 style="margin-top:1.5rem">{esc(label)} ({len(group)})</h3>')
        for f in group:
            body.append(_finding_card(f, charts.get(id(f), "")))

    body.append("<h2>What the data was taken to be</h2>")
    body.append(_columns_table(audit))
    notes = _notes(audit)
    if notes:
        body.append("<h2>Questions, limits, and observations</h2>")
        body.append(notes)
    body.append("<h2>Check register</h2>")
    body.append(_register())
    body.append(
        '<p class="foot">Generated locally by dataassay '
        f"{esc(p.tool_version)} on {esc(datetime.now(timezone.utc).date())}. "
        "Nothing in this file was uploaded anywhere. The machine-readable "
        "findings are embedded below as JSON.</p>"
    )
    body.append("</div>")

    payload = json.dumps(audit.to_dict(), indent=2, default=str)
    body.append(
        '<script type="application/json" id="dataassay-findings">'
        + payload.replace("</", "<\\/")
        + "</script>"
    )

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Audit — {esc(p.filename)}</title>"
        f"<style>{CSS}</style></head><body>"
        + "".join(body)
        + "</body></html>"
    )
