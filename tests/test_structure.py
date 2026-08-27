"""Structure inference. A wrong grain invalidates every check built on it."""

import duckdb

from dataassay.columns import profile_columns
from dataassay.structure import infer


def _infer(sql: str):
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE t AS {sql}")
    schema = [(r[0], r[1]) for r in con.execute("DESCRIBE t").fetchall()]
    (n,) = con.execute("SELECT count(*) FROM t").fetchone()
    cols = profile_columns(con, "t", [], schema, n)
    s = infer(cols, con, "t", [], n)
    con.close()
    return s


def test_single_temporal_column_is_the_axis():
    s = _infer("SELECT DATE '2026-01-01' + INTERVAL (i) DAY AS d, i AS v FROM range(0,30) t(i)")
    assert s.time_axis == "d"


def test_load_stamp_is_never_the_time_axis():
    # Picking run_date silently collapses every series to a flat line at the
    # fetch date, and nothing downstream would look wrong.
    s = _infer(
        "SELECT DATE '2026-01-01' + INTERVAL (i) DAY AS observation_date, "
        "DATE '2026-08-01' AS run_date, i AS v FROM range(0,30) t(i)"
    )
    assert s.time_axis == "observation_date"
    assert any("run_date" in a for a in s.assumptions)


def test_no_temporal_column_leaves_the_axis_unset():
    s = _infer("SELECT i AS a, i * 2 AS b FROM range(0,30) t(i)")
    assert s.time_axis is None
    assert "no temporal column" in s.time_axis_basis


def test_unique_column_is_taken_as_the_key():
    s = _infer("SELECT i AS id, i % 3 AS g FROM range(0,50) t(i)")
    assert s.grain == ["id"]
    assert s.grain_is_unique is True


def test_grain_adds_a_text_category():
    s = _infer(
        "SELECT DATE '2026-01-01' + INTERVAL (i // 2) DAY AS d, "
        "CASE WHEN i % 2 = 0 THEN 'a' ELSE 'b' END AS market, i AS v "
        "FROM range(0,40) t(i)"
    )
    assert s.grain == ["d", "market"]
    assert s.grain_is_unique is True
    assert s.group_columns == ["market"]


def test_integer_coded_category_counts_as_grain():
    # A marketing year is a number by type and a category by meaning. Excluding
    # integers made a legitimate year-boundary overlap look like duplicates.
    s = _infer(
        "SELECT DATE '2026-01-01' + INTERVAL (i // 2) DAY AS week_ending, "
        "(i % 2) + 2019 AS marketing_year, i AS v FROM range(0,40) t(i)"
    )
    assert s.grain == ["week_ending", "marketing_year"]
    assert s.grain_is_unique is True


def test_surrogate_key_does_not_replace_the_series_partition():
    # A unique id satisfies uniqueness and tells the ordered checks nothing.
    # They still need to know that (d, market) is what makes a series.
    s = _infer(
        "SELECT i AS row_id, DATE '2026-01-01' + INTERVAL (i // 2) DAY AS d, "
        "CASE WHEN i % 2 = 0 THEN 'a' ELSE 'b' END AS market "
        "FROM range(0,40) t(i)"
    )
    assert s.group_columns == ["market"]
    assert s.grain == ["d", "market"]


def test_unfindable_key_is_marked_exhausted_not_duplicated():
    s = _infer(
        "SELECT DATE '2026-01-01' AS d, 'x' AS a, i % 5 AS payload "
        "FROM range(0,50) t(i)"
    )
    assert s.grain_is_unique is False
    assert s.candidates_exhausted is True
