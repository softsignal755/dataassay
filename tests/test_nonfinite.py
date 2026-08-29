"""NaN and infinity: present in the file, but not numbers.

This whole file exists because of one real crash. `stddev_samp` raises "out of
range" on a column containing NaN, so an audit of livestock_prices.csv died
before it could report anything -- and the row that killed it, a whole line of
'nan' written by a failed fetch, was exactly the defect the tool exists to find.
A crash is the worst outcome, but silently folding NaN into the null count is
the second worst: a cell reading 'nan' is not an empty cell.
"""

from dataassay.audit import run
from dataassay.profile import build


def _rows(values, header="d,v"):
    out = [header]
    for i, v in enumerate(values):
        out.append(f"2026-{1 + i // 28:02d}-{(i % 28) + 1:02d},{v}")
    return "\n".join(out) + "\n"


class TestItDoesNotCrash:
    def test_a_nan_column_still_audits(self, write_csv):
        # The regression. Before the fix this raised OutOfRangeException.
        vals = [f"{100 + i}" for i in range(40)] + ["nan"]
        a = run(write_csv("a.csv", _rows(vals)))
        assert a.profile.columns[1].name == "v"

    def test_infinity_too(self, write_csv):
        vals = [f"{100 + i}" for i in range(40)] + ["inf", "-inf"]
        a = run(write_csv("a.csv", _rows(vals)))
        assert a.profile.columns[1].nonfinite == 2

    def test_statistics_come_from_the_finite_values(self, write_csv):
        vals = [f"{100 + i}" for i in range(40)] + ["nan"]
        col = build(write_csv("a.csv", _rows(vals))).columns[1]
        assert col.non_null == 40
        assert col.stddev is not None and col.stddev == col.stddev  # not NaN
        assert col.max_value == 139.0  # not nan


class TestItIsNotAMissingValue:
    def test_nan_is_counted_apart_from_nulls(self, write_csv):
        vals = [f"{100 + i}" for i in range(38)] + ["nan", ""]
        col = build(write_csv("a.csv", _rows(vals))).columns[1]
        assert (col.nonfinite, col.nulls, col.non_null) == (1, 1, 38)
        assert col.rows == col.nulls + col.nonfinite + col.non_null

    def test_it_is_reported_not_absorbed(self, write_csv):
        vals = [f"{100 + i}" for i in range(40)] + ["nan"]
        a = run(write_csv("a.csv", _rows(vals)))
        codes = {n.code for n in a.profile.notes}
        assert "non_finite_values" in codes

    def test_a_clean_file_says_nothing(self, write_csv):
        a = run(write_csv("a.csv", _rows([f"{100 + i}" for i in range(40)])))
        assert not any(n.code.startswith("non_finite") for n in a.profile.notes)
        assert a.profile.columns[1].nonfinite == 0


class TestOneRowIsOneFact:
    def test_columns_failing_together_collapse_to_one_question(self, write_csv):
        head = "d,a,b,c"
        rows = [head] + [
            f"2026-01-{i + 1:02d},{i},{i * 2},{i * 3}" for i in range(20)
        ] + ["2026-02-01,nan,nan,nan"]
        a = run(write_csv("a.csv", "\n".join(rows) + "\n"))
        blocks = [n for n in a.profile.notes if n.code == "nonfinite_block"]
        assert len(blocks) == 1
        assert blocks[0].evidence["columns"] == ["a", "b", "c"]
        # and no per-column repeat of the same fact
        assert not [n for n in a.profile.notes if n.code == "non_finite_values"]

    def test_disjoint_nans_are_not_called_one_block(self, write_csv):
        # Same count, different rows. Claiming these are one failure would be
        # a fabricated finding -- the exact trap _comissing_notes documents.
        head = "d,a,b"
        rows = [head] + [
            f"2026-01-{i + 1:02d},{i},{i * 2}" for i in range(1, 20)
        ] + ["2026-02-01,nan,5", "2026-02-02,7,nan"]
        a = run(write_csv("a.csv", "\n".join(rows) + "\n"))
        assert not [n for n in a.profile.notes if n.code == "nonfinite_block"]
        assert len([n for n in a.profile.notes
                    if n.code == "non_finite_values"]) == 2


class TestChecksDoNotReasonOverNaN:
    def test_no_finding_reports_a_nan_magnitude(self, write_csv):
        # The first fix stopped the crash but let NaN into the checks, which
        # then reported a level shift "by xnan". Nonsense output is a bug too.
        vals = [f"{100 + i}" for i in range(40)] + ["nan"]
        a = run(write_csv("a.csv", _rows(vals)))
        for f in a.findings:
            assert "nan" not in f.summary.lower()
