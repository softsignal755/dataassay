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
