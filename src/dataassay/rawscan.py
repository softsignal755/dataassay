"""The pre-parse pass: evidence that only exists in the raw bytes.

Two of the worst defects in tabular data are invisible after parsing, because
both produce *perfectly valid* values:

  decimal separator   "1.234" is 1234 under EU convention and 1.234 under US.
                      Read it wrong and you have a silent 1000x error that no
                      downstream type check will ever catch.

  date convention     "03/04/2026" is valid under both DD/MM and MM/DD and they
                      disagree. The two readings only diverge for days 1-12, so
                      a file can look fine for years and be wrong a third of the
                      time.

By the time DuckDB hands back typed columns the evidence is gone -- it has
already committed to one reading. So this pass runs first, on text, and its job
is to gather *evidence*, not to reach a verdict. Where the evidence does not
decide, the answer is AMBIGUOUS, and that is a question for the user rather than
a guess to be made quietly.

Parquet carries its own types, so this pass does not apply to it. That is
recorded as a reason, not skipped silently -- an inapplicable check is still a
statement about the dataset.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Cap the read so a huge file cannot stall the run. Whether the cap was hit is
# reported, because "no disambiguating value found" means much less when only
# part of the file was looked at.
BYTE_CAP = 64 * 1024 * 1024

DELIMITER_CANDIDATES = (",", ";", "\t", "|")

# Strings that conventionally stand in for "missing" and will otherwise parse as
# a legitimate category value.
STRING_SENTINELS = frozenset(
    {"na", "n/a", "#n/a", "null", "none", "nil", "-", "--", "?", "nan", "unknown", "."}
)

# -- numeric shape -------------------------------------------------------------
# A value is decisive only when it CANNOT be read the other way. Grouping alone
# is not enough: "1.234" is a single group under EU and a plain decimal under US,
# which is exactly the trap. What settles it is more than one group
# ("1.234.567"), a second separator carrying the fraction ("1.234,56"), a
# fraction that is not three digits long (no convention groups 1, 2, or 4+
# digits), or a leading part too long to be a first group ("1234.567").
_EU_DECISIVE = re.compile(
    r"^[+-]?\d{1,3}(\.\d{3}){2,}$"      # 1.234.567
    r"|^[+-]?\d{1,3}(\.\d{3})+,\d+$"    # 1.234,56
    r"|^[+-]?\d+,\d{1,2}$"              # 12,5
    r"|^[+-]?\d+,\d{4,}$"               # 12,3456
    r"|^[+-]?\d{4,},\d{3}$"             # 1234,567
)
_US_DECISIVE = re.compile(
    r"^[+-]?\d{1,3}(,\d{3}){2,}$"       # 1,234,567
    r"|^[+-]?\d{1,3}(,\d{3})+\.\d+$"    # 1,234.56
    r"|^[+-]?\d+\.\d{1,2}$"             # 12.5
    r"|^[+-]?\d+\.\d{4,}$"              # 12.3456
    r"|^[+-]?\d{4,}\.\d{3}$"            # 1234.567
)
# The trap: one separator, exactly three digits after it, and a leading part
# short enough to be a group. Valid under both readings; they differ by 1000x.
_AMBIGUOUS = re.compile(r"^[+-]?\d{1,3}[.,]\d{3}$")
_PLAIN_INT = re.compile(r"^[+-]?\d+$")

# -- date shape ----------------------------------------------------------------
_ISO = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}([T ].*)?$")
_TWO_PART = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$")


@dataclass
class FieldEvidence:
    """Raw-text tallies for one column. Counts, never conclusions."""

    name: str
    values: int = 0
    blank: int = 0
    string_sentinels: Counter = field(default_factory=Counter)
    max_length: int = 0
    untrimmed: int = 0

    eu_decisive: int = 0
    us_decisive: int = 0
    grouping_ambiguous: int = 0
    plain_int: int = 0

    iso_dates: int = 0
    two_part_dates: int = 0
    first_gt_12: int = 0
    second_gt_12: int = 0
    two_digit_year: int = 0

    example_ambiguous: str | None = None
    example_ambiguous_date: str | None = None

    # ---- verdicts derived from the tallies ----

    @property
    def decimal_separator(self) -> str:
        if self.eu_decisive and self.us_decisive:
            return "conflicting"
        if self.eu_decisive:
            return ","
        if self.us_decisive:
            return "."
        if self.grouping_ambiguous:
            return "ambiguous"
        return "not-applicable"

    @property
    def date_convention(self) -> str:
        if not (self.iso_dates or self.two_part_dates):
            return "not-applicable"
        if self.iso_dates and not self.two_part_dates:
            return "ISO-8601"
        if self.first_gt_12 and self.second_gt_12:
            return "conflicting"
        if self.first_gt_12:
            return "DD/MM"
        if self.second_gt_12:
            return "MM/DD"
        return "ambiguous"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "values": self.values,
            "blank": self.blank,
            "string_sentinels": dict(self.string_sentinels),
            "max_length": self.max_length,
            "untrimmed": self.untrimmed,
            "decimal_separator": self.decimal_separator,
            "decimal_evidence": {
                "eu_decisive": self.eu_decisive,
                "us_decisive": self.us_decisive,
                "grouping_ambiguous": self.grouping_ambiguous,
                "plain_int": self.plain_int,
                "example": self.example_ambiguous,
            },
            "date_convention": self.date_convention,
            "date_evidence": {
                "iso": self.iso_dates,
                "two_part": self.two_part_dates,
                "first_gt_12": self.first_gt_12,
                "second_gt_12": self.second_gt_12,
                "two_digit_year": self.two_digit_year,
                "example": self.example_ambiguous_date,
            },
        }


@dataclass
class RawScan:
    applicable: bool
    reason: str = ""
    encoding: str = ""
    byte_order_mark: bool = False
    delimiter: str = ""
    delimiter_confidence: float = 0.0
    header: list[str] = field(default_factory=list)
    data_rows: int = 0
    ragged_rows: list[int] = field(default_factory=list)
    truncated: bool = False
    bytes_scanned: int = 0
    fields: list[FieldEvidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        if not self.applicable:
            return {"applicable": False, "reason": self.reason}
        return {
            "applicable": True,
            "encoding": self.encoding,
            "byte_order_mark": self.byte_order_mark,
            "delimiter": self.delimiter,
            "delimiter_confidence": round(self.delimiter_confidence, 3),
            "data_rows": self.data_rows,
            "ragged_rows": self.ragged_rows[:20],
            "ragged_row_count": len(self.ragged_rows),
            "truncated": self.truncated,
            "bytes_scanned": self.bytes_scanned,
            "fields": [f.to_dict() for f in self.fields],
        }


def _decode(raw: bytes) -> tuple[str, str, bool]:
    """Return (text, encoding_label, had_bom). Never raises: a file we cannot
    decode strictly is still worth scanning, and the encoding itself is a
    finding."""
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    try:
        return body.decode("utf-8"), ("utf-8-sig" if bom else "utf-8"), bom
    except UnicodeDecodeError:
        # latin-1 cannot fail, so this is a deliberate fallback rather than a
        # detection. The label says so.
        return body.decode("latin-1"), "latin-1 (utf-8 decode failed)", bom


def _sniff_delimiter(lines: list[str]) -> tuple[str, float]:
    """Pick the delimiter that yields the most *consistent* field count.

    csv.Sniffer is opaque about why it chose what it chose; an audit tool has to
    be able to show its work, so this uses a stated rule: the winner is the
    candidate whose modal field count covers the largest share of sample lines,
    with ties broken by more fields.
    """
    sample = [ln for ln in lines[:200] if ln.strip()]
    if not sample:
        return ",", 0.0

    best, best_score, best_fields = ",", 0.0, 0
    for cand in DELIMITER_CANDIDATES:
        counts = Counter(len(next(csv.reader([ln], delimiter=cand))) for ln in sample)
        n_fields, hits = counts.most_common(1)[0]
        if n_fields < 2:
            continue
        score = hits / len(sample)
        if (score, n_fields) > (best_score, best_fields):
            best, best_score, best_fields = cand, score, n_fields
    return best, best_score


def _tally(ev: FieldEvidence, value: str) -> None:
    raw = value
    v = value.strip()
    if raw != v:
        ev.untrimmed += 1
    if not v:
        ev.blank += 1
        return

    ev.values += 1
    ev.max_length = max(ev.max_length, len(v))
    if v.lower() in STRING_SENTINELS:
        ev.string_sentinels[v] += 1
        return

    if _EU_DECISIVE.match(v):
        ev.eu_decisive += 1
    elif _US_DECISIVE.match(v):
        ev.us_decisive += 1
    elif _AMBIGUOUS.match(v):
        ev.grouping_ambiguous += 1
        if ev.example_ambiguous is None:
            ev.example_ambiguous = v
    elif _PLAIN_INT.match(v):
        ev.plain_int += 1
        return

    if _ISO.match(v):
        ev.iso_dates += 1
        return
    m = _TWO_PART.match(v)
    if m:
        ev.two_part_dates += 1
        a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if a > 12:
            ev.first_gt_12 += 1
        if b > 12:
            ev.second_gt_12 += 1
        if len(y) == 2:
            ev.two_digit_year += 1
        if a <= 12 and b <= 12 and ev.example_ambiguous_date is None:
            ev.example_ambiguous_date = v


def scan(path: Path, reader: str, byte_cap: int = BYTE_CAP) -> RawScan:
    if reader != "read_csv":
        return RawScan(
            applicable=False,
            reason="Parquet carries its own types; separator and date-convention "
            "ambiguity cannot arise and no raw-text pass is needed.",
        )

    with path.open("rb") as fh:
        raw = fh.read(byte_cap + 1)
    truncated = len(raw) > byte_cap
    if truncated:
        # Drop the partial final line so a split value is not miscounted.
        raw = raw[:byte_cap].rpartition(b"\n")[0]

    text, encoding, bom = _decode(raw)
    lines = text.splitlines()
    if not lines:
        return RawScan(applicable=False, reason="file is empty")

    delimiter, confidence = _sniff_delimiter(lines)
    rows = csv.reader(lines, delimiter=delimiter)

    try:
        header = next(rows)
    except StopIteration:
        return RawScan(applicable=False, reason="file has no rows")
    header = [h.strip().lstrip("﻿") for h in header]

    evidence = [FieldEvidence(name=h or f"column_{i}") for i, h in enumerate(header)]
    ragged: list[int] = []
    data_rows = 0

    for lineno, row in enumerate(rows, start=2):
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        data_rows += 1
        if len(row) != len(header):
            ragged.append(lineno)
            continue
        for ev, value in zip(evidence, row, strict=True):
            _tally(ev, value)

    return RawScan(
        applicable=True,
        encoding=encoding,
        byte_order_mark=bom,
        delimiter=delimiter,
        delimiter_confidence=confidence,
        header=header,
        data_rows=data_rows,
        ragged_rows=ragged,
        truncated=truncated,
        bytes_scanned=len(raw),
        fields=evidence,
    )
