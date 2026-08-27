"""Composite time axes, row order, and staleness — all earned by the backtest."""

from datetime import date, timedelta

from dataassay.audit import run


def _find(audit, check_id):
    return [f for f in audit.findings if f.check_id == check_id]


class TestCompositeTimeAxis:
    def test_year_month_dekad_is_assembled(self, write_csv):
        rows = ["region,year,month,dekad,v"]
        for i in range(60):
            y, m, d = 2020 + i // 36, (i // 3) % 12 + 1, i % 3 + 1
            rows.append(f"br,{y},{m},{d},{i}")
        s = run(write_csv("a.csv", "\n".join(rows) + "\n")).structure
        assert s.time_parts == ["year", "month", "dekad"]
        assert "make_date" in s.time_sql

    def test_year_plus_month_name_is_assembled(self, write_csv):
        # "END OF APR" is a date to a human and nothing at all to a parser.
        months = ["END OF JAN", "END OF FEB", "END OF MAR", "END OF APR",
                  "END OF MAY", "END OF JUN"]
        rows = ["year,period,v"]
        for y in (2024, 2025, 2026):
            for i, m in enumerate(months):
                rows.append(f"{y},{m},{i}")
        s = run(write_csv("a.csv", "\n".join(rows) + "\n")).structure
        assert s.time_parts == ["year", "period"]
        assert "CASE" in s.time_sql

    def test_an_assembled_axis_unlocks_the_ordered_checks(self, write_csv):
        rows = ["year,month,v"]
        for i in range(40):
            rows.append(f"{2020 + i // 12},{i % 12 + 1},{i}")
        a = run(write_csv("a.csv", "\n".join(rows) + "\n"))
        withheld = dict(a.coverage.withheld)
        assert "level_shift" not in withheld
        assert "cadence_gap" not in withheld

    def test_a_real_date_column_still_wins(self, write_csv):
        rows = ["d,year,month,v"]
        for i in range(40):
            rows.append(f"2026-01-{(i % 28) + 1:02d},2026,1,{i}")
        s = run(write_csv("a.csv", "\n".join(rows) + "\n")).structure
        assert s.time_axis == "d"
        assert s.time_sql == ""

    def test_a_bare_year_is_not_enough(self, write_csv):
        rows = ["year,v"] + [f"{2000 + i},{i}" for i in range(30)]
        s = run(write_csv("a.csv", "\n".join(rows) + "\n")).structure
        assert s.time_sql == ""


class TestFileOrder:
    def test_alphabetical_month_order_is_a_defect(self, write_csv):
        # H7 exactly: sorted by month NAME, so the last row is March.
        months = ["END OF APR", "END OF AUG", "END OF DEC", "END OF FEB",
                  "END OF JAN", "END OF JUL", "END OF JUN", "END OF MAR",
                  "END OF MAY", "END OF NOV", "END OF OCT", "END OF SEP"]
        rows = ["year,period,v"]
        for y in (2024, 2025, 2026):
            for i, m in enumerate(months):
                rows.append(f"{y},{m},{i}")
        f = _find(run(write_csv("a.csv", "\n".join(rows) + "\n")), "file_order")
        assert f and f[0].disposition == "defect"
        assert f[0].evidence["last_is_latest"] is False

    def test_a_file_in_time_order_is_silent(self, write_csv):
        rows = ["d,v"] + [
            f"2026-01-{(i % 28) + 1:02d},{i}" for i in range(28)
        ]
        assert not _find(run(write_csv("a.csv", "\n".join(rows) + "\n")), "file_order")

    def test_a_panel_grouped_by_series_is_not_a_defect(self, write_csv):
        # Two interleaved series, each in order, laid out one after the other.
        # Globally "out of order" and perfectly fine.
        rows = ["d,market,v"]
        for m in ("a", "b"):
            for i in range(20):
                rows.append(f"2026-01-{i + 1:02d},{m},{i}")
        f = _find(run(write_csv("a.csv", "\n".join(rows) + "\n")), "file_order")
        assert f and f[0].disposition == "suspect"
        assert f[0].evidence["grouped_panel"] is True
        assert f[0].evidence["inversions_within_series"] == 0

    def test_disorder_inside_a_series_is_not_excused_by_the_panel(self, write_csv):
        rows = ["d,market,v"]
        for i in range(20):
            rows.append(f"2026-01-{i + 1:02d},a,{i}")
        rows.append("2026-01-05,a,99")  # backwards within series a
        for i in range(20):
            rows.append(f"2026-01-{i + 1:02d},b,{i}")
        f = _find(run(write_csv("a.csv", "\n".join(rows) + "\n")), "file_order")
        assert f and f[0].evidence["inversions_within_series"] > 0


class TestStaleTail:
    def test_a_frozen_feed_is_caught_against_its_own_load_stamp(self, write_csv):
        # The file was written today; the data stopped months ago. A freshness
        # check on the file's timestamp sees nothing wrong.
        rows = ["d,run_date,v"]
        start = date(2026, 1, 1)
        for i in range(20):
            rows.append(f"{start + timedelta(days=7 * i)},2026-08-01,{i}")
        f = _find(run(write_csv("a.csv", "\n".join(rows) + "\n")), "stale_tail")
        assert f and f[0].disposition == "defect"
        assert f[0].evidence["stamp_column"] == "run_date"

    def test_a_current_series_is_silent(self, write_csv):
        rows = ["d,run_date,v"]
        today = date.today()
        for i in range(30, 0, -1):
            rows.append(f"{today - timedelta(days=7 * i)},{today},{31 - i}")
        rows.append(f"{today},{today},99")
        assert not _find(run(write_csv("a.csv", "\n".join(rows) + "\n")), "stale_tail")

    def test_without_a_load_stamp_it_is_only_suspect(self, write_csv):
        rows = ["d,v"]
        start = date(2026, 1, 1)
        for i in range(30):
            rows.append(f"{start + timedelta(days=7 * i)},{i}")
        f = _find(run(write_csv("a.csv", "\n".join(rows) + "\n")), "stale_tail")
        assert f and f[0].disposition == "suspect"
        assert "only true as of today" in " ".join(f[0].confidence.inputs)
