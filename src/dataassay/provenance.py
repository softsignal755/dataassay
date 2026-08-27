"""Provenance: identify exactly which bytes were audited.

Every report leads with this block. An audit that cannot be tied back to an
exact file is worthless the moment the file is regenerated, so the content hash
is not decoration -- it is the primary key of a finding.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from dataassay import __version__

# Extension -> DuckDB table function. Dispatch is explicit rather than relying on
# the replacement scan so that an unsupported format fails with our message
# instead of a DuckDB parse error.
_READERS = {
    ".parquet": "read_parquet",
    ".pq": "read_parquet",
    ".csv": "read_csv",
    ".tsv": "read_csv",
    ".txt": "read_csv",
}

_HASH_CHUNK = 1024 * 1024


class UnsupportedFormat(ValueError):
    pass


def reader_for(path: Path) -> str:
    try:
        return _READERS[path.suffix.lower()]
    except KeyError:
        supported = ", ".join(sorted(set(_READERS)))
        raise UnsupportedFormat(
            f"{path.name}: no reader for '{path.suffix}' (supported: {supported})"
        ) from None


def content_hash(path: Path) -> str:
    """SHA-256 of the file bytes, streamed so file size is irrelevant."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Column:
    name: str
    declared_type: str


def source_expr(reader: str, lenient: bool = False) -> str:
    """The table function to read this file with, as a bindable expression."""
    if lenient and reader == "read_csv":
        return "read_csv(?, ignore_errors=true)"
    return f"{reader}(?)"


@dataclass(frozen=True)
class Provenance:
    path: str
    filename: str
    size_bytes: int
    content_sha256: str
    reader: str
    row_count: int
    columns: list[Column]
    audited_at: str
    tool: str
    tool_version: str
    # True when a strict read failed and rows had to be skipped to proceed. A
    # file that cannot be parsed is a finding, not a crash.
    lenient: bool = False

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["column_count"] = self.column_count
        return d


def describe(path: Path) -> Provenance:
    """Read the file's shape without loading it into memory.

    DESCRIBE and count(*) both push down to DuckDB, so this stays cheap on a
    file larger than RAM -- the property the whole engine is built on.
    """
    reader = reader_for(path)
    con = duckdb.connect(":memory:")
    lenient = False
    try:
        for attempt_lenient in (False, True):
            src = source_expr(reader, attempt_lenient)
            try:
                described = con.execute(
                    f"DESCRIBE SELECT * FROM {src}", [str(path)]
                ).fetchall()
                (row_count,) = con.execute(
                    f"SELECT count(*) FROM {src}", [str(path)]
                ).fetchone()
            except duckdb.InvalidInputException:
                if attempt_lenient or reader != "read_csv":
                    raise
                continue
            lenient = attempt_lenient
            break
        columns = [Column(name=row[0], declared_type=row[1]) for row in described]
    finally:
        con.close()

    return Provenance(
        path=str(path.resolve()),
        filename=path.name,
        size_bytes=path.stat().st_size,
        content_sha256=content_hash(path),
        reader=reader,
        row_count=int(row_count),
        columns=columns,
        audited_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        tool="dataassay",
        tool_version=__version__,
        lenient=lenient,
    )
