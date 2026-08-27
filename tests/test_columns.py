"""Measurements, and the gate that decides which checks are valid at all."""

import duckdb
import pytest

from dataassay.columns import kind_of, profile_columns


def _profile(rows_sql: str):
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE t AS {rows_sql}")
    schema = [(r[0], r[1]) for r in con.execute("DESCRIBE t").fetchall()]
    (n,) = con.execute("SELECT count(*) FROM t").fetchone()
    out = {c.name: c for c in profile_columns(con, "t", [], schema, n)}
    con.close()
    return out


def _props(col):
    return {p["property"]: p for p in col.observed_properties()}


class TestKindDispatch:
    @pytest.mark.parametrize(
        ("declared", "expected"),
        [
            ("BIGINT", "numeric"), ("DOUBLE", "numeric"), ("DECIMAL(18,3)", "numeric"),
            ("DATE", "temporal"), ("TIMESTAMP", "temporal"),
            ("BOOLEAN", "boolean"), ("VARCHAR", "text"),
        ],
    )
    def test_declared_types_map_to_kinds(self, declared, expected):
        assert kind_of(declared) == expected


class TestSigmaGate:
    def test_well_behaved_column_supports_sigma(self):
        cols = _profile("SELECT i::DOUBLE AS v FROM range(0, 200) t(i)")
        p = _props(cols["v"])["sigma_establishable"]
        assert p["holds"] is True

    def test_heavy_tail_disqualifies_sigma(self):
        # One extreme value inflates stddev far past the robust scale. A 3-sigma
        # rule here is not weak, it is invalid -- so the gate must close.
        cols = _profile(
            "SELECT i::DOUBLE AS v FROM range(0, 200) t(i) "
            "UNION ALL SELECT 1e9"
        )
        p = _props(cols["v"])["sigma_establishable"]
        assert p["holds"] is False
        assert "robust scale" in p["evidence"]

    def test_too_few_values_disqualifies_sigma(self):
        cols = _profile("SELECT i::DOUBLE AS v FROM range(0, 5) t(i)")
        p = _props(cols["v"])["sigma_establishable"]
        assert p["holds"] is False
        assert "need 30" in p["evidence"]

    def test_zero_dispersion_disqualifies_sigma(self):
        cols = _profile("SELECT 7.0::DOUBLE AS v FROM range(0, 100) t(i)")
        p = _props(cols["v"])["sigma_establishable"]
        assert p["holds"] is False
        assert "IQR is zero" in p["evidence"]


class TestSentinels:
    def test_sentinel_at_the_extreme_is_flagged(self):
        cols = _profile(
            "SELECT i::DOUBLE AS v FROM range(0, 50) t(i) UNION ALL SELECT -999"
        )
        assert "-999.0" in cols["v"].sentinel_candidates

    def test_sentinel_value_mid_distribution_is_not_flagged(self):
        # -99 inside a range that reaches -5000 is far more likely to be a real
        # observation than a missing-value code.
        cols = _profile(
            "SELECT i::DOUBLE AS v FROM range(-5000, 5000) t(i) UNION ALL SELECT -99"
        )
        assert cols["v"].sentinel_candidates == {}


class TestObservedProperties:
    def test_candidate_key_is_identified(self):
        cols = _profile("SELECT i AS id FROM range(0, 100) t(i)")
        assert _props(cols["id"])["candidate_key"]["holds"] is True

    def test_constant_column_is_identified(self):
        cols = _profile("SELECT 5 AS v FROM range(0, 100) t(i)")
        assert _props(cols["v"])["constant"]["holds"] is True

    def test_all_null_column_short_circuits(self):
        cols = _profile("SELECT NULL::DOUBLE AS v FROM range(0, 10) t(i)")
        props = _props(cols["v"])
        assert props["all_null"]["holds"] is True
        assert "sigma_establishable" not in props
        assert cols["v"].quantiles == {}

    def test_integral_and_sign_are_measured(self):
        cols = _profile("SELECT i::DOUBLE AS v FROM range(1, 50) t(i)")
        props = _props(cols["v"])
        assert props["integral"]["holds"] is True
        assert props["non_negative"]["holds"] is True

    def test_fractional_values_defeat_integral(self):
        cols = _profile("SELECT (i + 0.5)::DOUBLE AS v FROM range(1, 50) t(i)")
        assert _props(cols["v"])["integral"]["holds"] is False

    def test_percent_range_notes_whether_the_name_agrees(self):
        cols = _profile("SELECT (i % 100)::DOUBLE AS mm_pct_oi, "
                        "(i % 100)::DOUBLE AS amount FROM range(0, 200) t(i)")
        assert "name says so" in _props(cols["mm_pct_oi"])["percent_range"]["evidence"]
        assert "nothing but the range" in _props(cols["amount"])["percent_range"]["evidence"]


class TestMeasurement:
    def test_quantiles_and_topk_are_populated(self):
        cols = _profile("SELECT (i % 4)::VARCHAR AS g, i::DOUBLE AS v "
                        "FROM range(0, 100) t(i)")
        assert cols["v"].quantiles["0.5"] == pytest.approx(49.5, abs=1.0)
        assert len(cols["g"].top_values) == 4

    def test_high_cardinality_column_skips_topk(self):
        cols = _profile("SELECT i::VARCHAR AS v FROM range(0, 500) t(i)")
        assert cols["v"].top_values == []

    def test_null_fraction_is_reported(self):
        cols = _profile(
            "SELECT CASE WHEN i < 90 THEN NULL ELSE i::DOUBLE END AS v "
            "FROM range(0, 100) t(i)"
        )
        assert cols["v"].null_fraction == pytest.approx(0.9)
        assert _props(cols["v"])["high_null"]["holds"] is True
