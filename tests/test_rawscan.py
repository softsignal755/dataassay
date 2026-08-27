"""The raw pass exists for defects that survive parsing. These are those."""

from dataassay.rawscan import scan

HEAD = "id,amount,when\n"


def _fields(path):
    s = scan(path, "read_csv")
    return {f.name: f for f in s.fields}


class TestDecimalSeparator:
    def test_three_digit_group_is_ambiguous_not_guessed(self, write_csv):
        # "1.234" is 1234 under EU and 1.234 under US. Both parse. The tool must
        # refuse to pick rather than introduce a silent 1000x error.
        p = write_csv("a.csv", HEAD + "1,1.234,2026-01-01\n2,5.678,2026-01-02\n")
        assert _fields(p)["amount"].decimal_separator == "ambiguous"

    def test_eu_grouping_with_comma_decimal_is_decisive(self, write_csv):
        p = write_csv("a.csv", HEAD + '1,"1.234.567,89",2026-01-01\n')
        assert _fields(p)["amount"].decimal_separator == ","

    def test_us_grouping_with_dot_decimal_is_decisive(self, write_csv):
        p = write_csv("a.csv", HEAD + '1,"1,234,567.89",2026-01-01\n')
        assert _fields(p)["amount"].decimal_separator == "."

    def test_non_three_digit_fraction_settles_it(self, write_csv):
        # "12,5" cannot be grouping: no convention makes a 1-digit group.
        p = write_csv("a.csv", HEAD + '1,"12,5",2026-01-01\n')
        assert _fields(p)["amount"].decimal_separator == ","

    def test_mixed_conventions_are_reported_as_conflicting(self, write_csv):
        p = write_csv("a.csv", HEAD + '1,"1.234,56",2026-01-01\n2,"1,234.56",2026-01-02\n')
        assert _fields(p)["amount"].decimal_separator == "conflicting"

    def test_long_leading_part_cannot_be_a_group(self, write_csv):
        # "1234.567" has four leading digits, so the dot cannot be grouping.
        p = write_csv("a.csv", HEAD + "1,1234.567,2026-01-01\n")
        assert _fields(p)["amount"].decimal_separator == "."

    def test_multiple_groups_are_decisive(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,1.234.567,2026-01-01\n")
        assert _fields(p)["amount"].decimal_separator == ","

    def test_plain_integers_raise_no_question(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,1234,2026-01-01\n2,5678,2026-01-02\n")
        assert _fields(p)["amount"].decimal_separator == "not-applicable"


class TestDateConvention:
    def test_all_days_under_13_is_ambiguous(self, write_csv):
        # The dangerous case: valid both ways, disagreeing, for years at a time.
        p = write_csv("a.csv", HEAD + "1,10,03/04/2026\n2,20,05/06/2026\n")
        f = _fields(p)["when"]
        assert f.date_convention == "ambiguous"
        assert f.example_ambiguous_date == "03/04/2026"

    def test_day_over_12_in_first_position_settles_dd_mm(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,10,25/03/2026\n2,20,03/04/2026\n")
        assert _fields(p)["when"].date_convention == "DD/MM"

    def test_day_over_12_in_second_position_settles_mm_dd(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,10,03/25/2026\n")
        assert _fields(p)["when"].date_convention == "MM/DD"

    def test_both_positions_over_12_is_conflicting(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,10,25/03/2026\n2,20,03/25/2026\n")
        assert _fields(p)["when"].date_convention == "conflicting"

    def test_iso_needs_no_question(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,10,2026-03-04\n")
        assert _fields(p)["when"].date_convention == "ISO-8601"

    def test_two_digit_years_are_counted(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,10,25/03/26\n")
        assert _fields(p)["when"].two_digit_year == 1


class TestStructure:
    def test_semicolon_delimiter_is_detected(self, write_csv):
        p = write_csv("a.csv", "id;amount\n1;2\n3;4\n")
        s = scan(p, "read_csv")
        assert s.delimiter == ";"
        assert s.delimiter_confidence == 1.0

    def test_ragged_rows_are_located_by_line_number(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,10,2026-01-01\n2,20\n3,30,2026-01-03\n")
        s = scan(p, "read_csv")
        assert s.ragged_rows == [3]

    def test_undecodable_bytes_fall_back_and_say_so(self, write_csv):
        p = write_csv("a.csv", "id,city\n1,S\xe3o Paulo\n", encoding="latin-1")
        s = scan(p, "read_csv")
        assert s.encoding.startswith("latin-1")

    def test_bom_is_stripped_from_the_first_header(self, write_csv):
        p = write_csv("a.csv", "﻿id,amount\n1,2\n")
        s = scan(p, "read_csv")
        assert s.byte_order_mark is True
        assert s.header[0] == "id"

    def test_truncation_is_reported_so_absence_means_less(self, write_csv):
        body = "".join(f"{i},{i},2026-01-01\n" for i in range(500))
        p = write_csv("a.csv", HEAD + body)
        s = scan(p, "read_csv", byte_cap=200)
        assert s.truncated is True
        assert s.data_rows < 500

    def test_string_sentinels_are_tallied_not_treated_as_categories(self, write_csv):
        p = write_csv("a.csv", "id,grade\n1,NA\n2,N/A\n3,B\n")
        f = _fields(p)["grade"]
        assert f.string_sentinels["NA"] == 1
        assert f.string_sentinels["N/A"] == 1

    def test_parquet_is_not_applicable_with_a_stated_reason(self, tmp_path):
        s = scan(tmp_path / "x.parquet", "read_parquet")
        assert s.applicable is False
        assert "Parquet" in s.reason
