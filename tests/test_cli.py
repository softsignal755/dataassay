import json

import pytest

from dataassay import __version__
from dataassay.cli import main
from dataassay.provenance import UnsupportedFormat, content_hash, describe, reader_for

CSV = "date,state,value\n2026-01-01,MG,1.5\n2026-01-02,SP,2.5\n"


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "sample.csv"
    p.write_text(CSV)
    return p


def test_version_is_set():
    assert __version__.count(".") == 2


def test_reader_dispatch(tmp_path):
    assert reader_for(tmp_path / "a.parquet") == "read_parquet"
    assert reader_for(tmp_path / "a.CSV") == "read_csv"
    with pytest.raises(UnsupportedFormat):
        reader_for(tmp_path / "a.xlsx")


def test_content_hash_is_stable_and_content_addressed(csv_file, tmp_path):
    twin = tmp_path / "renamed.csv"
    twin.write_text(CSV)
    assert content_hash(csv_file) == content_hash(twin)

    different = tmp_path / "other.csv"
    different.write_text(CSV.replace("1.5", "1.6"))
    assert content_hash(different) != content_hash(csv_file)


def test_describe_reports_shape(csv_file):
    prov = describe(csv_file)
    assert prov.row_count == 2
    assert prov.column_count == 3
    assert [c.name for c in prov.columns] == ["date", "state", "value"]
    assert prov.reader == "read_csv"
    assert prov.tool_version == __version__


def test_profile_json_round_trips(csv_file, capsys):
    assert main(["profile", str(csv_file), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provenance"]["row_count"] == 2
    assert payload["provenance"]["column_count"] == 3


def test_profile_text_lists_columns(csv_file, capsys):
    assert main(["profile", str(csv_file)]) == 0
    out = capsys.readouterr().out
    assert "sample.csv" in out
    assert "state" in out


def test_missing_file_is_exit_2(tmp_path, capsys):
    assert main(["profile", str(tmp_path / "nope.csv")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_unsupported_format_is_exit_2(tmp_path, capsys):
    p = tmp_path / "book.xlsx"
    p.write_bytes(b"nope")
    assert main(["profile", str(p)]) == 2
    assert "no reader" in capsys.readouterr().err
