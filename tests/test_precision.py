"""Residue vs. data, and precision as a provenance signal."""

import duckdb

from dataassay.audit import run
from dataassay.checks.base import DEFECT, SUSPECT
from dataassay.columns import profile_columns


def _series(tmp_path, values, name="v"):
    rows = [f"d,{name}"]
    for i, v in enumerate(values):
        rows.append(f"2026-{1 + i // 28:02d}-{(i % 28) + 1:02d},{v!r}")
    p = tmp_path / "s.csv"
    p.write_text("\n".join(rows) + "\n")
    return p


def _residue(audit):
    return [f for f in audit.findings if f.check_id == "negligible_residue"]


class TestNegligibleResidue:
    def test_cluster_below_a_gap_is_a_defect(self, tmp_path):
        vals = [-7.345364920210326e-22] * 40 + [round(1.0 + i / 10, 4) for i in range(20)]
        f = _residue(run(_series(tmp_path, vals)))
        assert f and f[0].disposition == DEFECT
        assert f[0].evidence["distinct_in_cluster"] == 1
        assert f[0].evidence["gap_decades"] >= 6

    def test_the_consequence_is_stated_not_just_the_anomaly(self, tmp_path):
        # The point is not that the values are odd. It is that `= 0` is false.
        vals = [-7.3e-22] * 40 + [round(1.0 + i / 10, 4) for i in range(20)]
        assert "= 0` is false" in _residue(run(_series(tmp_path, vals)))[0].summary

    def test_genuine_multi_decade_data_is_not_flagged(self, tmp_path):
        # Concentrations, probabilities, trace readings: the smallest values are
        # the interesting ones. A continuum has no gap.
        vals = [10.0 ** (-e / 2) for e in range(40)]
        assert not _residue(run(_series(tmp_path, vals)))

    def test_a_varied_small_cluster_is_only_suspect(self, tmp_path):
        # Residue repeats; a spread of distinct tiny values might be real.
        vals = [1e-20 * (i + 1) for i in range(40)]
        vals += [round(1.0 + i / 10, 4) for i in range(20)]
        f = _residue(run(_series(tmp_path, vals)))
        assert f and f[0].disposition == SUSPECT

    def test_ordinary_column_is_silent(self, tmp_path):
        assert not _residue(run(_series(tmp_path, [round(i * 1.5, 2) for i in range(40)])))

    def test_zeros_are_not_residue(self, tmp_path):
        # A real zero is excluded from the magnitude histogram entirely.
        vals = [0.0] * 30 + [round(1.0 + i / 10, 4) for i in range(30)]
        assert not _residue(run(_series(tmp_path, vals)))


def _props(sql: str) -> dict:
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE t AS {sql}")
    schema = [(r[0], r[1]) for r in con.execute("DESCRIBE t").fetchall()]
    (n,) = con.execute("SELECT count(*) FROM t").fetchone()
    cols = profile_columns(con, "t", [], schema, n)
    con.close()
    return {
        c.name: {p["property"]: p for p in c.observed_properties()} for c in cols
    }


class TestDerivedPrecision:
    def test_computed_column_is_marked_derived(self):
        # A source does not report 15 significant digits; arithmetic does.
        p = _props("SELECT (i / 7.0) AS v FROM range(0, 100) t(i)")
        assert p["v"]["derived_precision"]["holds"] is True
        assert "computed rather than reported" in p["v"]["derived_precision"]["evidence"]

    def test_reported_column_is_not(self):
        p = _props("SELECT round(i * 1.25, 2) AS v FROM range(0, 100) t(i)")
        assert "derived_precision" not in p["v"]

    def test_integers_are_not_derived(self):
        p = _props("SELECT i::DOUBLE AS v FROM range(0, 100) t(i)")
        assert "derived_precision" not in p["v"]

    def test_it_is_a_property_not_a_finding(self, tmp_path):
        # Precision says where a column came from. That is provenance, not a
        # fault, and filing it as one would bury the faults.
        vals = [i / 7.0 for i in range(60)]
        a = run(_series(tmp_path, vals))
        assert not [f for f in a.findings if "precision" in f.check_id]
        col = next(c for c in a.profile.columns if c.name == "v")
        assert any(
            p["property"] == "derived_precision" for p in col.observed_properties()
        )
