"""Assembly, and the promise that what could not be established is reported."""

import json

from dataassay.profile import LIMITATION, QUESTION, build

HEAD = "id,amount,when\n"


def _codes(profile, severity=None):
    return {n.code for n in profile.notes if severity is None or n.severity == severity}


class TestQuestionsAreRaised:
    def test_ambiguous_decimal_is_a_question_when_the_delimiter_agrees(self, write_csv):
        # A semicolon delimiter is itself the European tell, so an unresolved
        # ambiguity there is a real question.
        p = write_csv("a.csv", "id;amount;when\n1;1.234;2026-01-01\n2;5.678;2026-01-02\n")
        assert "decimal_separator_ambiguous" in _codes(build(p), QUESTION)

    def test_ambiguity_is_assumed_not_asked_when_no_comma_exists(self, write_csv):
        # Comma-delimited and no comma in any value: comma cannot be the decimal
        # separator, so the reading is assumed and shown rather than asked.
        p = write_csv("a.csv", HEAD + "1,1.234,2026-01-01\n2,5.678,2026-01-02\n")
        prof = build(p)
        assert prof.questions == []
        assert "decimal_separator_assumed" in _codes(prof)

    def test_one_decisive_value_settles_the_whole_file(self, write_csv):
        # "12.5" cannot be grouping, so every ambiguous value in the file
        # inherits the "." reading -- one fact, not one question per column.
        p = write_csv("a.csv", HEAD + "1,1.234,2026-01-01\n2,12.5,2026-01-02\n")
        prof = build(p)
        assert prof.questions == []
        assert "decimal_separator_resolved" in _codes(prof)

    def test_conflicting_conventions_are_asked_once_for_the_file(self, write_csv):
        p = write_csv(
            "a.csv", HEAD + '1,"1.234,56",2026-01-01\n2,"1,234.56",2026-01-02\n'
        )
        prof = build(p)
        assert _codes(prof, QUESTION) == {"decimal_separator_conflicting"}

    def test_ambiguous_date_becomes_a_question(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,10,03/04/2026\n2,20,05/06/2026\n")
        assert "date_convention_ambiguous" in _codes(build(p), QUESTION)

    def test_clean_iso_file_asks_nothing(self, write_csv):
        body = "".join(f"{i},{i * 3},2026-01-01\n" for i in range(1, 60))
        prof = build(write_csv("a.csv", HEAD + body))
        assert prof.questions == []

    def test_high_null_column_becomes_a_question(self, write_csv):
        body = "".join(
            f"{i},{'' if i % 10 else i},2026-01-01\n" for i in range(1, 100)
        )
        prof = build(write_csv("a.csv", HEAD + body))
        assert "high_null" in _codes(prof, QUESTION)

    def test_ragged_rows_become_a_question(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,10,2026-01-01\n2,20\n")
        assert "ragged_rows" in _codes(build(p), QUESTION)


class TestLimitationsAreReported:
    def test_unusable_sigma_is_recorded_as_a_limitation(self, write_csv):
        body = "".join(f"{i},{i},2026-01-01\n" for i in range(1, 100))
        p = write_csv("a.csv", HEAD + body + "999,1000000000,2026-01-01\n")
        prof = build(p)
        assert "sigma_not_establishable" in _codes(prof, LIMITATION)

    def test_truncation_is_recorded_so_absence_means_less(self, write_csv):
        body = "".join(f"{i},{i},2026-01-01\n" for i in range(1, 400))
        prof = build(write_csv("a.csv", HEAD + body), byte_cap=300)
        assert "rawscan_truncated" in _codes(prof, LIMITATION)


class TestParquet:
    def test_parquet_skips_the_raw_pass_with_a_reason(self, write_parquet):
        p = write_parquet("a.parquet", "SELECT i AS id, i * 1.5 AS v FROM range(0, 100) t(i)")
        prof = build(p)
        assert prof.rawscan.applicable is False
        assert "rawscan_not_applicable" in _codes(prof)
        assert len(prof.columns) == 2

    def test_parquet_columns_are_still_measured(self, write_parquet):
        p = write_parquet("a.parquet", "SELECT i::DOUBLE AS v FROM range(0, 200) t(i)")
        prof = build(p)
        assert prof.columns[0].quantiles["0.5"] > 0


class TestMachineContract:
    def test_profile_serializes_to_json(self, write_csv):
        p = write_csv("a.csv", HEAD + "1,1.234,03/04/2026\n")
        payload = json.loads(json.dumps(build(p).to_dict(), default=str))
        assert payload["schema_version"] == 1
        assert payload["provenance"]["row_count"] == 1
        assert {c["name"] for c in payload["columns"]} == {"id", "amount", "when"}
        assert payload["rawscan"]["applicable"] is True

    def test_high_cardinality_text_never_reaches_the_payload(self, write_csv):
        # The LLM adapter is handed exactly this dict. Lexical min/max on a text
        # column are raw cell values -- a customer name or an account number --
        # so a high-cardinality column must contribute nothing but counts.
        body = "".join(f"{i},SECRET-{i},2026-01-01\n" for i in range(1, 200))
        prof = build(write_csv("a.csv", HEAD + body))
        payload = json.dumps(prof.to_dict(), default=str)
        assert "SECRET" not in payload
        col = next(c for c in prof.to_dict()["columns"] if c["name"] == "amount")
        assert col["lexical_extremes_withheld"] is True
        assert col["max_length"] == 10  # the signal survives; the value does not

    def test_low_cardinality_categories_survive_and_are_flagged(self, write_csv):
        # Characterizing a categorical column without its categories is not
        # possible, so this exception is deliberate -- and declared in the
        # payload rather than silent.
        body = "".join(f"{i},{'red' if i % 2 else 'blue'},2026-01-01\n" for i in range(1, 60))
        prof = build(write_csv("a.csv", HEAD + body))
        col = next(c for c in prof.to_dict()["columns"] if c["name"] == "amount")
        assert col["top_values_are_raw_values"] is True
        assert {v for v, _ in col["top_values"]} == {"red", "blue"}


class TestComissingBlocks:
    def test_columns_missing_on_the_same_rows_collapse_to_one_question(self, write_csv):
        head = "id,a,b,c\n"
        body = "".join(
            f"{i}," + ("1,2,3\n" if i % 2 else ",,\n") for i in range(1, 41)
        )
        prof = build(write_csv("a.csv", head + body))
        blocks = [n for n in prof.notes if n.code == "comissing_block"]
        assert len(blocks) == 1
        assert set(blocks[0].evidence["columns"]) == {"a", "b", "c"}
        # and the per-column repetition is suppressed
        assert not [n for n in prof.notes if n.code == "high_null"]

    def test_equally_sparse_but_disjoint_columns_are_not_merged(self, write_csv):
        # The trap: identical null counts, complementary rows. Calling these one
        # block would be a fabricated finding.
        head = "id,heat,cold\n"
        body = "".join(
            f"{i}," + ("5,\n" if i % 2 else ",7\n") for i in range(1, 41)
        )
        prof = build(write_csv("a.csv", head + body))
        assert [n for n in prof.notes if n.code == "comissing_block"] == []
        assert {n.column for n in prof.notes if n.code == "high_null"} == {"heat", "cold"}

    def test_two_complementary_blocks_are_reported_separately(self, write_csv):
        head = "id,h1,h2,c1,c2\n"
        body = "".join(
            f"{i}," + ("1,2,,\n" if i % 2 else ",,3,4\n") for i in range(1, 41)
        )
        prof = build(write_csv("a.csv", head + body))
        blocks = [n for n in prof.notes if n.code == "comissing_block"]
        assert len(blocks) == 2
        assert {frozenset(b.evidence["columns"]) for b in blocks} == {
            frozenset({"h1", "h2"}), frozenset({"c1", "c2"})
        }
