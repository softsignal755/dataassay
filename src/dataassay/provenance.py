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


# How hard to try before giving up on a CSV, in order. The middle rung exists
# because DuckDB's sniffer samples a prefix: a file whose only quoted fields
# appear past that window is sniffed as having NO quote character, and every
# quoted comma then splits a row into an extra column. On one real file that
# silently dropped 1,232 of 50,947 rows -- and the tool reported them as a
# defect in the user's data, which is precisely the failure it exists to catch.
# RFC 4180 says the quote character is `"`, so saying so explicitly is not a
# guess; it is the standard the sniffer was trying to infer.
STRICT, QUOTED, LENIENT = "strict", "quoted", "lenient"
READ_MODES = (STRICT, QUOTED, LENIENT)


def source_expr(reader: str, mode: str = STRICT) -> str:
    """The table function to read this file with, as a bindable expression."""
    if reader != "read_csv" or mode == STRICT:
        return f"{reader}(?)"
    if mode == QUOTED:
        return "read_csv(?, quote='\"')"
    return "read_csv(?, quote='\"', ignore_errors=true)"


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
    # How the file had to be read. "strict" is a clean parse; "quoted" means the
    # sniffer's guess was wrong and the standard quote character recovered it;
    # "lenient" means rows had to be SKIPPED, which is a finding, not a crash.
    read_mode: str = "strict"

    @property
    def lenient(self) -> bool:
        return self.read_mode == "lenient"

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
    mode = STRICT
    try:
        for attempt in READ_MODES:
            src = source_expr(reader, attempt)
            try:
                described = con.execute(
                    f"DESCRIBE SELECT * FROM {src}", [str(path)]
                ).fetchall()
                (row_count,) = con.execute(
                    f"SELECT count(*) FROM {src}", [str(path)]
                ).fetchone()
            except duckdb.InvalidInputException:
                if attempt == LENIENT or reader != "read_csv":
                    raise
                continue
            mode = attempt
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
        read_mode=mode,
    )
