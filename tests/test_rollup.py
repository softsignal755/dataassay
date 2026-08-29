"""Roll-up: the same catalog, one grain coarser.

The tests that matter here are the refusals. A roll-up is a SUM, and a sum is a
claim that adding two rows of a column means something -- so the cases worth
pinning are the ones where the tool declines to sum, declines to run a check
that has no meaning on an aggregate, and declines to repeat itself.
"""

from datetime import date, timedelta

import pytest

from dataassay import rollup as rollup_mod
from dataassay.audit import run


def _panel(reporters=("BRA", "COL"), partners=("USA", "DEU"), months=30,
           header="month,reporter,partner,qty_kg,value_usd", bump=None,
           extra_col=None):
    """A small trade panel: one row per reporter x partner x month."""
    rows = [header]
    for i in range(months):
        d = date(2023, 1, 1) + timedelta(days=31 * i)
        for r in reporters:
            for p in partners:
                q = 1000.0 + 7 * i + 13 * len(r + p)
                if bump and bump(r, p, i):
                    q *= 1000
                cells = [d.isoformat(), r, p, f"{q:.1f}", f"{q * 4:.1f}"]
                if extra_col is not None:
                    cells.append(extra_col)
                rows.append(",".join(cells))
    return "\n".join(rows) + "\n"


class TestLevels:
    def test_it_leaves_one_key_out_at_a_time(self, write_csv):
        a = run(write_csv("t.csv", _panel()), rollup=True)
        dropped = sorted(r.level.dropped[0] for r in a.rollups)
        assert dropped == ["partner", "reporter"]

    def test_the_aggregate_is_smaller_than_the_file(self, write_csv):
        a = run(write_csv("t.csv", _panel()), rollup=True)
        for r in a.rollups:
            assert 0 < r.level.rows < r.level.source_rows

    def test_the_rolled_grain_is_unique_by_construction(self, write_csv):
        # A GROUP BY cannot produce a duplicate key. If this ever reports
        # otherwise the aggregate is not grouping on what it claims to.
        a = run(write_csv("t.csv", _panel()), rollup=True)
        for r in a.rollups:
            assert r.structure.grain_is_unique

    def test_provenance_stays_the_file(self, write_csv):
        # The rolled rows were computed, but they were computed from these
        # bytes -- the hash still identifies what was audited.
        a = run(write_csv("t.csv", _panel()), rollup=True)
        for r in a.rollups:
            assert (r.profile.provenance.content_sha256
                    == a.profile.provenance.content_sha256)

    def test_nothing_runs_unless_asked(self, write_csv):
        a = run(write_csv("t.csv", _panel()))
        assert a.rollups == [] and not a.rollup_withheld


class TestAdditivity:
    def test_a_price_column_is_never_summed(self, write_csv):
        head = "month,reporter,partner,qty_kg,unit_price"
        a = run(write_csv("t.csv", _panel(header=head)), rollup=True)
        by_name = {m.name: m.agg for m in a.rollups[0].level.measures}
        assert by_name["qty_kg"] == "sum"
        assert by_name["unit_price"] == "mean"

    def test_the_choice_says_why(self, write_csv):
        head = "month,reporter,partner,qty_kg,share_pct"
        a = run(write_csv("t.csv", _panel(header=head)), rollup=True)
        basis = {m.name: m.basis for m in a.rollups[0].level.measures}
        assert "not a quantity at all" in basis["share_pct"]
        assert "manifest" in basis["share_pct"]

    def test_a_declared_answer_overrides_the_name(self, write_csv):
        # 'balance' reads as a level, but a trade balance in dollars adds
        # perfectly well. A person saying so must win over a regex.
        from dataassay import manifest as manifest_mod
        path = write_csv("t.csv", _panel(
            header="month,reporter,partner,qty_kg,balance"))
        m = manifest_mod.Manifest(declared={"additive": ["balance"]})
        m.write(manifest_mod.path_for(path))
        a = run(path, rollup=True)
        by_name = {m.name: m.agg for m in a.rollups[0].level.measures}
        assert by_name["balance"] == "sum"

    def test_a_column_declared_none_is_dropped(self, write_csv):
        from dataassay import manifest as manifest_mod
        path = write_csv("t.csv", _panel())
        m = manifest_mod.Manifest(declared={"aggregate": {"value_usd": "none"}})
        m.write(manifest_mod.path_for(path))
        a = run(path, rollup=True)
        names = {m_.name for m_ in a.rollups[0].level.measures}
        assert "value_usd" not in names and "qty_kg" in names
        assert any(c == "value_usd" for c, _ in a.rollups[0].level.excluded)

    def test_nothing_aggregable_means_nothing_to_roll_up(self, write_csv):
        from dataassay import manifest as manifest_mod
        path = write_csv("t.csv", _panel())
        m = manifest_mod.Manifest(declared={
            "aggregate": {"qty_kg": "none", "value_usd": "none"}})
        m.write(manifest_mod.path_for(path))
        a = run(path, rollup=True)
        assert a.rollups == []
        assert "nothing to carry" in a.rollup_withheld


class TestRefusals:
    def test_a_file_with_no_series_key_says_so(self, write_csv):
        rows = ["d,v"] + [
            f"{(date(2023,1,1) + timedelta(days=i)).isoformat()},{i}"
            for i in range(40)
        ]
        a = run(write_csv("t.csv", "\n".join(rows) + "\n"), rollup=True)
        assert a.rollups == []
        assert "no series key" in a.rollup_withheld

    def test_file_order_is_withheld_on_an_aggregate(self, write_csv):
        # There is no such thing as the order a GROUP BY's rows are stored in.
        a = run(write_csv("t.csv", _panel()), rollup=True)
        withheld = dict(a.rollups[0].coverage.withheld)
        assert "file_order" in withheld
        assert "aggregate" in withheld["file_order"]
        # and it is still a live check on the file itself
        assert "file_order" not in dict(a.coverage.withheld)

    def test_a_level_never_repeats_the_file_level_finding(self, write_csv):
        a = run(write_csv("t.csv", _panel(
            bump=lambda r, p, i: r == "BRA" and p == "USA" and i >= 15)),
            rollup=True)
        base = {(f.check_id, f.column, f.summary) for f in a.findings}
        for r in a.rollups:
            for f in r.findings:
                assert (f.check_id, f.column, f.summary) not in base


class TestItWouldFailIfBroken:
    """The mutation half: prove these assertions can fail."""

    def test_summing_an_intensive_column_would_be_caught(self, write_csv):
        head = "month,reporter,partner,qty_kg,unit_price"
        a = run(write_csv("t.csv", _panel(header=head)), rollup=True)
        # If classify_measures started summing it, the mean would become a sum
        # and the aggregate would carry a number that is not a price.
        aggs = {m.name: m.agg for m in a.rollups[0].level.measures}
        assert aggs == {"qty_kg": "sum", "unit_price": "mean"}

    def test_the_aggregate_actually_aggregates(self, write_csv):
        # A roll-up that quietly passed the file through would have the same
        # row count, and every finding would be identical to the file's.
        a = run(write_csv("t.csv", _panel()), rollup=True)
        by_drop = {r.level.dropped[0]: r.level.rows for r in a.rollups}
        assert by_drop["partner"] == 30 * 2   # reporters x months
        assert by_drop["reporter"] == 30 * 2  # partners x months


class TestClassifier:
    """Mean is the DEFAULT, and that asymmetry is the point.

    Guessing SUM on an intensive column invents a number that means nothing.
    Guessing MEAN on an extensive one only produces a less natural summary. So
    the extensive list is deliberately narrow and everything else averages.
    """

    @pytest.mark.parametrize("name", [
        "qty_kg", "value_usd", "tonnes", "bags", "head", "acres",
        "total_exports", "production",
    ])
    def test_amounts_are_summed(self, name):
        assert rollup_mod.EXTENSIVE_NAME.search(name)

    @pytest.mark.parametrize("name", [
        "unit_price", "avg_yield", "pct_share", "fx_rate", "temperature_2m",
        "close", "stocks_index", "soil_moisture_l2", "cwsi_z",
        "normalized_et", "avg_dewpoint_c", "crop_water_stress_index",
    ])
    def test_everything_else_averages(self, name):
        assert not rollup_mod.EXTENSIVE_NAME.search(name)

    def test_an_unknown_name_averages_rather_than_sums(self, write_csv):
        # The safety property: a name the tool has never seen must not be
        # summed on a guess.
        a = run(write_csv("t.csv", _panel(
            header="month,reporter,partner,qty_kg,widgetiness")), rollup=True)
        by_name = {m.name: m.agg for m in a.rollups[0].level.measures}
        assert by_name["widgetiness"] == "mean"
        assert by_name["qty_kg"] == "sum"
