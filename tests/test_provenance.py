"""Provenance is the primary key of a finding, not decoration."""

import pytest

from dataassay import __version__
from dataassay.provenance import UnsupportedFormat, content_hash, describe, reader_for

CSV = "date,state,value\n2026-01-01,MG,1.5\n2026-01-02,SP,2.5\n"


@pytest.fixture
def csv_file(write_csv):
    return write_csv("sample.csv", CSV)


def test_version_is_a_triple():
    assert __version__.count(".") == 2


def test_reader_dispatch(tmp_path):
    assert reader_for(tmp_path / "a.parquet") == "read_parquet"
    assert reader_for(tmp_path / "a.CSV") == "read_csv"
    with pytest.raises(UnsupportedFormat):
        reader_for(tmp_path / "a.xlsx")


def test_hash_is_content_addressed_not_name_addressed(csv_file, write_csv):
    assert content_hash(csv_file) == content_hash(write_csv("renamed.csv", CSV))
    assert content_hash(write_csv("other.csv", CSV.replace("1.5", "1.6"))) != content_hash(
        csv_file
    )


def test_describe_reports_shape(csv_file):
    prov = describe(csv_file)
    assert prov.row_count == 2
    assert prov.column_count == 3
    assert [c.name for c in prov.columns] == ["date", "state", "value"]
    assert prov.reader == "read_csv"
    assert prov.tool_version == __version__


class TestReadModes:
    def test_a_quoted_field_beyond_the_sniff_window_is_recovered(self, tmp_path):
        """The sniffer samples a prefix. A file whose only quoted fields appear
        past it is sniffed as having NO quote character, and every quoted comma
        then splits a row. On a real 50,947-row file that silently dropped
        1,232 rows — and the tool called them a defect in the user's data."""
        p = tmp_path / "late_quotes.csv"
        rows = ["a,b,c"]
        rows += [f"{i},plain,{i * 2}" for i in range(30_000)]
        rows.append('99999,"has, a comma",7')
        p.write_text("\n".join(rows) + "\n")

        prov = describe(p)
        assert prov.row_count == 30_001
        assert prov.column_count == 3
        assert prov.read_mode == "quoted"
        assert prov.lenient is False

    def test_an_ordinary_file_still_reads_strict(self, write_csv):
        prov = describe(write_csv("a.csv", "a,b\n1,2\n3,4\n"))
        assert prov.read_mode == "strict"
        assert prov.lenient is False

    def test_genuinely_broken_rows_still_fall_through_to_lenient(self, write_csv):
        body = "a,b\n" + "".join(f"{i},{i}\n" for i in range(50))
        body += '3,"unterminated\n'
        prov = describe(write_csv("b.csv", body))
        assert prov.row_count > 0
