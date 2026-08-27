import duckdb
import pytest


@pytest.fixture
def write_csv(tmp_path):
    def _write(name: str, text: str, encoding: str = "utf-8"):
        p = tmp_path / name
        p.write_bytes(text.encode(encoding))
        return p

    return _write


@pytest.fixture
def write_parquet(tmp_path):
    def _write(name: str, select: str):
        p = tmp_path / name
        con = duckdb.connect(":memory:")
        con.execute(f"COPY ({select}) TO '{p}' (FORMAT PARQUET)")
        con.close()
        return p

    return _write
