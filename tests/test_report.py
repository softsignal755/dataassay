"""The report: one file, no network, and the machine contract inside it."""

import json
import re

import pytest

from dataassay import report as report_mod
from dataassay.audit import run


def _series_csv(tmp_path, values, name="v"):
    rows = [f"d,{name}"]
    for i, v in enumerate(values):
        rows.append(f"2026-{1 + i // 28:02d}-{(i % 28) + 1:02d},{v!r}")
    p = tmp_path / "s.csv"
    p.write_text("\n".join(rows) + "\n")
    return p


@pytest.fixture
def built(tmp_path):
    # A file with a residue cluster: gives the report something to chart.
    vals = [-7.345364920210326e-22] * 40 + [round(1.0 + i / 10, 4) for i in range(30)]
    data = _series_csv(tmp_path, vals)
    audit = run(data)
    html_path, csv_path = report_mod.build(audit, data, tmp_path / "out.html")
    return audit, html_path, csv_path, html_path.read_text()


class TestSelfContained:
    def test_no_external_requests(self, built):
        # The whole pitch is that nothing leaves the machine. A single CDN link
        # in the output would contradict it on the artifact itself.
        _, _, _, html = built
        assert "http://" not in html
        assert "https://" not in html
        assert "<script src" not in html

    def test_it_is_one_file(self, built):
        _, html_path, _, html = built
        assert html_path.exists()
        assert "<style>" in html and "</style>" in html


class TestMachineContract:
    def test_findings_json_is_embedded_and_parses(self, built):
        audit, _, _, html = built
        m = re.search(
            r'<script type="application/json" id="dataassay-findings">(.*?)</script>',
            html, re.S,
        )
        assert m
        payload = json.loads(m.group(1).replace("<\\/", "</"))
        assert len(payload["findings"]) == len(audit.findings)
        assert payload["provenance"]["content_sha256"]
        assert payload["coverage"]["checks_total"] >= 9

    def test_the_two_cannot_drift(self, built):
        # Embedding rather than writing a sibling file is the point: the human
        # artifact and the machine contract are the same bytes.
        audit, _, _, html = built
        for f in audit.findings:
            assert f.check_id in html


class TestFlaggedCsv:
    def test_one_row_per_finding(self, built):
        import csv

        audit, _, csv_path, _ = built
        with csv_path.open() as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == len(audit.findings)
        assert rows[0]["predicate"]
        assert rows[0]["disposition"] in {"defect", "suspect", "bookkeeping"}


class TestCharts:
    def test_a_residue_finding_gets_its_chart(self, built):
        _, _, _, html = built
        assert '<svg class="chart"' in html
        assert "should be zero" in html  # the direct label, not just colour

    def test_charts_carry_a_legend_where_two_categories_share_one(self, built):
        _, _, _, html = built
        assert ">residue<" in html and ">measured<" in html

    def test_geometry_stays_inside_the_viewbox(self, built):
        _, _, _, html = built
        for svg in re.findall(r'<svg class="chart".*?</svg>', html, re.S):
            w, h = (float(x) for x in
                    re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups())
            xs = [float(v) for v in re.findall(r'(?:x|cx|x1|x2)="(-?[\d.]+)"', svg)]
            ys = [float(v) for v in re.findall(r'(?:y|cy|y1|y2)="(-?[\d.]+)"', svg)]
            assert all(-1 <= v <= w + 1 for v in xs)
            assert all(-1 <= v <= h + 1 for v in ys)

    def test_no_nan_reaches_the_output(self, built):
        _, _, _, html = built
        assert "NaN" not in html and "Infinity" not in html

    def test_a_finding_with_no_chart_still_renders(self, tmp_path):
        # A chart is an aid, never the finding.
        data = tmp_path / "d.csv"
        data.write_text("a,b\n" + "".join(f"x,{i % 3}\n" for i in range(30)) + "x,0\n")
        audit = run(data)
        html_path, _ = report_mod.build(audit, data, tmp_path / "o.html")
        html = html_path.read_text()
        assert "Findings" in html
        for f in audit.findings:
            assert f.summary[:40] in html or f.check_id in html


class TestTheme:
    def test_both_dark_scopes_are_defined(self, built):
        # The OS setting and the viewer's toggle are different signals and the
        # toggle must win both ways.
        _, _, _, html = built
        assert "@media (prefers-color-scheme: dark)" in html
        assert ':root[data-theme="dark"]' in html
        assert ':root:not([data-theme="light"])' in html

    def test_body_paints_its_own_background(self, built):
        _, _, _, html = built
        assert "background: var(--surface-page)" in html


class TestStructure:
    def test_coverage_appears_before_findings(self, built):
        # An empty findings list is uninterpretable without it.
        _, _, _, html = built
        assert html.index("Coverage") < html.index(">Findings")

    def test_provenance_carries_the_hash(self, built):
        audit, _, _, html = built
        assert audit.profile.provenance.content_sha256 in html

    def test_every_finding_shows_its_predicate(self, built):
        audit, _, _, html = built
        assert html.count("Reproduce this without our code") == len(audit.findings)
