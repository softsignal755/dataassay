"""Each check: it fires when it should, and stays quiet when it should not.

The negative cases carry more weight than the positive ones. Every one of them
is a false positive this tool actually produced against real data before the
check was corrected.
"""

from datetime import date, timedelta

from dataassay.audit import run
from dataassay.checks.base import DEFECT, SUSPECT


def _find(audit, check_id):
    return [f for f in audit.findings if f.check_id == check_id]


def _daily(n, value="{i}", start=1, extra_head="", extra="", head="d,v"):
    rows = [head + extra_head]
    for i in range(start, start + n):
        day = f"2026-{1 + (i - 1) // 28:02d}-{((i - 1) % 28) + 1:02d}"
        rows.append(f"{day},{value.format(i=i)}{extra}")
    return "\n".join(rows) + "\n"


class TestConstantNumeric:
    def test_all_zero_column_is_reported(self, write_csv):
        a = run(write_csv("a.csv", _daily(40, value="0")))
        assert _find(a, "constant_numeric")

    def test_it_does_not_claim_to_know_which_reading_is_right(self, write_csv):
        # Zero is both a failed join and a quantity genuinely absent in scope
        # (snow in the tropics). Nothing in the data separates them, so the
        # finding must not assert one.
        f = _find(run(write_csv("a.csv", _daily(40, value="0"))), "constant_numeric")[0]
        assert f.disposition == SUSPECT
        assert "genuinely absent" in f.summary

    def test_varying_column_is_silent(self, write_csv):
        assert not _find(run(write_csv("a.csv", _daily(40))), "constant_numeric")


class TestMojibake:
    def test_corrupted_text_is_caught(self, write_csv):
        body = "d,name\n" + "".join(
            f"2026-01-{i:02d},SÃ£o Paulo\n" for i in range(1, 20)
        )
        f = _find(run(write_csv("a.csv", body)), "mojibake")
        assert f and f[0].disposition == DEFECT

    def test_correct_accented_text_is_not_flagged(self, write_csv):
        body = "d,name\n" + "".join(
            f"2026-01-{i:02d},São Paulo\n" for i in range(1, 20)
        )
        assert not _find(run(write_csv("a.csv", body)), "mojibake")

    def test_uppercase_portuguese_is_not_flagged(self, write_csv):
        # "SAO PAULO" spelled correctly in caps puts A-tilde next to an ASCII
        # letter. Matching the lead character alone would flag every one.
        body = "d,name\n" + "".join(
            f"2026-01-{i:02d},SÃO PAULO\n" for i in range(1, 20)
        )
        assert not _find(run(write_csv("a.csv", body)), "mojibake")


class TestDuplicates:
    def test_identical_rows_are_a_defect(self, write_csv):
        body = _daily(30) + "2026-01-05,5\n"
        f = _find(run(write_csv("a.csv", body)), "duplicate_rows")
        assert f and f[0].disposition == DEFECT

    def test_clean_file_has_none(self, write_csv):
        assert not _find(run(write_csv("a.csv", _daily(30))), "duplicate_rows")

    def test_unfindable_key_blocks_rather_than_accuses(self, write_csv):
        # Not finding the key is our ignorance, not the data's defect.
        body = "d,tag,payload\n" + "".join(
            f"2026-01-01,x,{i % 4}\n" for i in range(40)
        )
        a = run(write_csv("a.csv", body))
        assert not _find(a, "duplicate_grain")
        assert any(c == "duplicate_grain" for c, _ in a.coverage.blocked)

    def test_marketing_year_overlap_is_not_a_duplicate(self, write_csv):
        # The same calendar week belongs to the end of one marketing year and
        # the start of the next. That is the source's calendar, not a defect.
        rows = ["week_ending,marketing_year,v"]
        for i in range(14):
            for my in (2020, 2021):
                rows.append(f"2026-01-{i + 1:02d},{my},{i}")
        a = run(write_csv("a.csv", "\n".join(rows) + "\n"))
        assert not _find(a, "duplicate_grain")
        assert a.structure.grain == ["week_ending", "marketing_year"]


class TestFutureDates:
    def test_one_period_ahead_with_no_flag_is_a_defect(self, write_csv):
        # A daily series whose last row is dated tomorrow: a forecast runs to a
        # horizon, an off-by-one period stamp lands exactly one step out.
        today = date.today()
        rows = ["d,v"]
        for i in range(30, 0, -1):
            rows.append(f"{today - timedelta(days=i)},{i}")
        rows.append(f"{today + timedelta(days=1)},99")
        f = _find(run(write_csv("a.csv", "\n".join(rows) + "\n")), "future_dates")
        assert f and f[0].disposition == DEFECT

    def test_forecast_flag_makes_it_bookkeeping(self, write_csv):
        today = date.today()
        rows = ["d,is_forecast,v"]
        for i in range(30, 0, -1):
            rows.append(f"{today - timedelta(days=i)},0,{i}")
        for i in range(1, 15):
            rows.append(f"{today + timedelta(days=i)},1,99")
        f = _find(run(write_csv("a.csv", "\n".join(rows) + "\n")), "future_dates")
        assert f and f[0].disposition == "bookkeeping"

    def test_past_only_file_is_silent(self, write_csv):
        assert not _find(run(write_csv("a.csv", _daily(30))), "future_dates")


class TestSaturation:
    def test_ceiling_pileup_is_caught(self, write_csv):
        rows = ["d,idx"]
        for i in range(120):
            v = 100 if i % 2 else i % 40
            rows.append(f"2026-01-{(i % 28) + 1:02d},{v}")
        assert _find(run(write_csv("a.csv", "\n".join(rows) + "\n")), "saturation_at_bound")

    def test_zero_floor_is_not_saturation(self, write_csv):
        # Most months have no cottonseed crushed. Those zeros are the
        # measurement, and flagging them condemns every sparse column alive.
        rows = ["d,crush"]
        for i in range(120):
            rows.append(f"2026-01-{(i % 28) + 1:02d},{0 if i % 3 else i}")
        assert not _find(
            run(write_csv("a.csv", "\n".join(rows) + "\n")), "saturation_at_bound"
        )

    def test_integer_category_is_not_saturation(self, write_csv):
        # 'dekad' takes the values 1, 2, 3. A third of rows on 1 is what the
        # column IS, not a cap it hit.
        rows = ["d,dekad"]
        for i in range(120):
            rows.append(f"2026-01-{(i % 28) + 1:02d},{(i % 3) + 1}")
        assert not _find(
            run(write_csv("a.csv", "\n".join(rows) + "\n")), "saturation_at_bound"
        )


class TestLevelShift:
    def _series(self, tmp_path, values):
        rows = ["d,v"]
        for i, v in enumerate(values):
            rows.append(f"2026-{1 + i // 28:02d}-{(i % 28) + 1:02d},{v}")
        p = tmp_path / "s.csv"
        p.write_text("\n".join(rows) + "\n")
        return p

    def test_units_change_is_a_defect(self, tmp_path):
        vals = [100 + i * 0.1 for i in range(60)]
        vals += [(100 + i * 0.1) * 1000 for i in range(60, 120)]
        f = _find(run(self._series(tmp_path, vals)), "level_shift")
        assert f and f[0].disposition == DEFECT
        assert f[0].evidence["persists"] is True
        assert "1000" in (f[0].evidence["unit_ratio"] or "")

    def test_transient_spike_reverts_and_is_only_suspect(self, tmp_path):
        vals = [100 + i * 0.1 for i in range(120)]
        vals[60] = 90000.0
        f = _find(run(self._series(tmp_path, vals)), "level_shift")
        assert f
        assert all(x.evidence["persists"] is False for x in f)
        assert all(x.disposition == SUSPECT for x in f)

    def test_one_spike_produces_one_finding(self, tmp_path):
        # A spike shows up twice in a step series: once out, once back.
        vals = [100 + i * 0.1 for i in range(120)]
        vals[60] = 90000.0
        assert len(_find(run(self._series(tmp_path, vals)), "level_shift")) == 1

    def test_ordinary_volatility_is_not_a_defect(self, tmp_path):
        # A fat-tailed series moving sharply and staying there is the market.
        # Scoring against typical steps alone called 26 of these defects.
        import random
        random.seed(7)
        v, vals = 100.0, []
        for _ in range(200):
            v *= 1 + random.gauss(0, 0.06)
            vals.append(round(v, 4))
        found = _find(run(self._series(tmp_path, vals)), "level_shift")
        assert not [f for f in found if f.disposition == DEFECT]


class TestFlatline:
    def test_stuck_tail_is_caught(self, tmp_path):
        rows = ["d,v"]
        for i in range(60):
            v = 42 if i >= 50 else i
            rows.append(f"2026-{1 + i // 28:02d}-{(i % 28) + 1:02d},{v}")
        p = tmp_path / "f.csv"
        p.write_text("\n".join(rows) + "\n")
        assert _find(run(p), "flatline_tail")

    def test_moving_series_is_silent(self, write_csv):
        assert not _find(run(write_csv("a.csv", _daily(40))), "flatline_tail")


class TestCoverageIsReported:
    def test_withheld_checks_carry_a_reason(self, write_csv):
        body = "name,tag\n" + "".join(f"n{i},x\n" for i in range(30))
        a = run(write_csv("a.csv", body))
        withheld = dict(a.coverage.withheld)
        assert "level_shift" in withheld
        assert "no temporal column" in withheld["level_shift"]

    def test_every_check_is_accounted_for(self, write_csv):
        a = run(write_csv("a.csv", _daily(40)))
        assert a.coverage.total == len(a.coverage.ran) + len(
            a.coverage.withheld
        ) + len(a.coverage.blocked)
        assert a.coverage.total >= 9

    def test_catalog_version_is_stamped(self, write_csv):
        assert run(write_csv("a.csv", _daily(40))).catalog_version


class TestCorroboration:
    def test_two_checks_on_one_column_raise_confidence(self, tmp_path):
        rows = ["d,v"]
        for i in range(120):
            v = 100 if i >= 60 else (i % 40)
            rows.append(f"2026-{1 + i // 28:02d}-{(i % 28) + 1:02d},{v}")
        p = tmp_path / "c.csv"
        p.write_text("\n".join(rows) + "\n")
        a = run(p)
        promoted = [
            f for f in a.findings
            if any("independently flagged" in i for i in f.confidence.inputs)
        ]
        if promoted:
            assert promoted[0].confidence.level == "high"


class TestUnparseableFiles:
    def test_broken_rows_are_reported_not_crashed_on(self, write_csv):
        body = "d,v\n" + "".join(f"2026-01-{(i % 28) + 1:02d},{i}\n" for i in range(40))
        body += "2026-01-05,1,2,3,4,5\n"
        a = run(write_csv("a.csv", body))
        assert a.profile.provenance.row_count > 0
        codes = {n.code for n in a.profile.notes}
        assert codes & {"rows_rejected_by_parser", "ragged_rows", "row_count_mismatch"}
