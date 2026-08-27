"""Assembly: provenance + raw-text evidence + typed measurement.

The Profile is the substrate everything downstream reads -- the checks in
Phase 2, the report in Phase 3, and the LLM adapter in Phase 5, which receives
exactly this and never a row of data.

It carries a `notes` list alongside the measurements. A note is something the
profiler could not settle on its own: an ambiguity that needs a human, or a
capability that is unavailable and therefore disables checks later. Recording
those here, at the moment they are discovered, is what lets the report state
what it could not check as plainly as what it found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from dataassay import columns as columns_mod
from dataassay import rawscan as rawscan_mod
from dataassay.provenance import Provenance, describe, reader_for

# Note severities. `question` is the interview queue: an answer changes which
# checks are valid, so it is worth a user's attention. `limitation` disables
# checks but has no answer to give. `observation` is neither.
QUESTION = "question"
LIMITATION = "limitation"
OBSERVATION = "observation"


@dataclass
class Note:
    severity: str
    code: str
    column: str | None
    message: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "column": self.column,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass
class Profile:
    provenance: Provenance
    rawscan: rawscan_mod.RawScan
    columns: list[columns_mod.ColumnProfile]
    notes: list[Note]

    @property
    def questions(self) -> list[Note]:
        return [n for n in self.notes if n.severity == QUESTION]

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "provenance": self.provenance.to_dict(),
            "rawscan": self.rawscan.to_dict(),
            "columns": [c.to_dict() for c in self.columns],
            "notes": [n.to_dict() for n in self.notes],
        }


def _decimal_notes(scan: rawscan_mod.RawScan) -> list[Note]:
    """Resolve the decimal convention ONCE, for the whole file.

    A producer does not switch conventions between columns, so evidence pools
    across them: a single decisive value anywhere settles every ambiguous value
    everywhere. Asking per column would put the same question in front of
    someone four times over for one fact.

    When nothing is decisive, the delimiter carries the argument. A
    comma-delimited file whose values contain no comma at all cannot be using
    comma as a decimal separator -- which leaves the dots as decimal points,
    since the alternative is a file of measured quantities with no fractional
    value anywhere. That is strong enough to assume and show rather than ask.
    A semicolon delimiter is the opposite tell, and there the question is real.
    """
    eu = sum(f.eu_decisive for f in scan.fields)
    us = sum(f.us_decisive for f in scan.fields)
    ambiguous = [f for f in scan.fields if f.grouping_ambiguous]
    total_ambiguous = sum(f.grouping_ambiguous for f in ambiguous)
    where = [f.name for f in ambiguous]
    example = next((f.example_ambiguous for f in ambiguous), None)

    if eu and us:
        return [Note(
            QUESTION, "decimal_separator_conflicting", None,
            "Both decimal conventions appear in this file; no single reading "
            "is correct for all of it.",
            {"eu_decisive": eu, "us_decisive": us},
        )]

    if not total_ambiguous:
        return []

    if us or eu:
        settled = "." if us else ","
        return [Note(
            OBSERVATION, "decimal_separator_resolved", None,
            f"{total_ambiguous} value(s) like {example!r} are ambiguous on their "
            f"own, but other values in this file are decisive, so the whole file "
            f"reads with '{settled}' as the decimal separator.",
            {"separator": settled, "ambiguous_values": total_ambiguous,
             "columns": where},
        )]

    if scan.delimiter == ",":
        return [Note(
            OBSERVATION, "decimal_separator_assumed", None,
            f"{total_ambiguous} value(s) like {example!r} would differ by 1000x "
            "under the other convention, and nothing in the file is decisive. "
            "Read as '.' because no value anywhere contains a comma, so comma "
            "is not the decimal separator here. Correct this if wrong — it "
            "moves every value in "
            + ", ".join(where[:4]) + (" and others" if len(where) > 4 else "")
            + " by a factor of 1000.",
            {"separator": ".", "assumed": True,
             "ambiguous_values": total_ambiguous, "columns": where},
        )]

    return [Note(
        QUESTION, "decimal_separator_ambiguous", None,
        f"{total_ambiguous} value(s) like {example!r} parse validly under both "
        f"conventions and differ by 1000x. Nothing in the file settles it, and "
        f"the {scan.delimiter!r} delimiter is itself commonly a European tell. "
        "Columns affected: " + ", ".join(where) + ".",
        {"ambiguous_values": total_ambiguous, "columns": where,
         "delimiter": scan.delimiter},
    )]


def _comissing_notes(
    con: duckdb.DuckDBPyConnection,
    source: str,
    params: list[str],
    cols: list[columns_mod.ColumnProfile],
) -> tuple[list[Note], set[str]]:
    """Collapse columns that go missing on the SAME rows into one finding.

    Six columns each 50% null is rarely six facts. It is usually one: a block of
    derived values that a whole class of rows never received. Asking about each
    separately buries the interesting part -- which rows, and why that block --
    under repetition.

    A shared null count is only a hint and is not enough. Columns can be equally
    sparse on completely disjoint rows: a livestock file that computes heat
    indices for one region and cold indices for another has two complementary
    blocks, and calling them one would be a fabricated finding. So the columns
    are partitioned by *actual* co-missingness, one query per block, before
    anything is claimed.
    """
    notes: list[Note] = []
    explained: set[str] = set()

    by_count: dict[int, list[columns_mod.ColumnProfile]] = {}
    for c in cols:
        if c.null_fraction > 0.2 and c.nulls > 0:
            by_count.setdefault(c.nulls, []).append(c)

    for nulls, group in sorted(by_count.items()):
        remaining = list(group)
        while len(remaining) > 1:
            anchor = remaining[0]
            exprs = ", ".join(
                f"count(*) FILTER (WHERE {columns_mod.q(anchor.name)} IS NULL "
                f"AND {columns_mod.q(c.name)} IS NULL) AS m{i}"
                for i, c in enumerate(remaining)
            )
            row = con.execute(f"SELECT {exprs} FROM {source}", params).fetchone()
            block = [c for c, hits in zip(remaining, row, strict=True) if hits == nulls]
            remaining = [c for c in remaining if c not in block]

            if len(block) < 2:
                continue
            names = [c.name for c in block]
            explained.update(names)
            notes.append(Note(
                QUESTION, "comissing_block", None,
                f"{len(names)} columns are absent together on the same "
                f"{nulls:,} row(s) ({block[0].null_fraction:.1%} of the file): "
                + ", ".join(names)
                + ". They look like one block that a whole class of rows never "
                "received. What distinguishes those rows?",
                {"columns": names, "rows_missing": nulls,
                 "null_fraction": round(block[0].null_fraction, 4)},
            ))

    return notes, explained


def _raw_notes(scan: rawscan_mod.RawScan) -> list[Note]:
    notes: list[Note] = []
    if not scan.applicable:
        notes.append(Note(OBSERVATION, "rawscan_not_applicable", None, scan.reason))
        return notes

    if scan.truncated:
        notes.append(Note(
            LIMITATION, "rawscan_truncated", None,
            f"Raw-text pass read the first {scan.bytes_scanned:,} bytes only. "
            "An absence of disambiguating values below is evidence about that "
            "prefix, not about the whole file.",
            {"bytes_scanned": scan.bytes_scanned},
        ))
    if scan.encoding.startswith("latin-1"):
        notes.append(Note(
            QUESTION, "encoding_fallback", None,
            "File is not valid UTF-8; it was read as latin-1. Confirm the "
            "encoding — a wrong one corrupts text values silently.",
            {"encoding": scan.encoding},
        ))
    if scan.delimiter_confidence < 0.95:
        notes.append(Note(
            QUESTION, "delimiter_uncertain", None,
            f"Delimiter {scan.delimiter!r} fits only "
            f"{scan.delimiter_confidence:.0%} of sampled lines.",
            {"delimiter": scan.delimiter, "confidence": scan.delimiter_confidence},
        ))
    if scan.ragged_rows:
        notes.append(Note(
            QUESTION, "ragged_rows", None,
            f"{len(scan.ragged_rows)} row(s) do not have {len(scan.header)} "
            "fields and were skipped by the raw pass.",
            {"first_lines": scan.ragged_rows[:10]},
        ))

    notes += _decimal_notes(scan)

    for ev in scan.fields:
        if ev.date_convention == "ambiguous":
            notes.append(Note(
                QUESTION, "date_convention_ambiguous", ev.name,
                f"Dates like {ev.example_ambiguous_date!r} are valid as both "
                "DD/MM and MM/DD; no value in the column exceeds 12 in either "
                "position to decide it.",
                {"two_part_dates": ev.two_part_dates,
                 "example": ev.example_ambiguous_date},
            ))
        elif ev.date_convention == "conflicting":
            notes.append(Note(
                QUESTION, "date_convention_conflicting", ev.name,
                "Some rows exceed 12 in the first position and others in the "
                "second — no single convention reads this column.",
                {"first_gt_12": ev.first_gt_12, "second_gt_12": ev.second_gt_12},
            ))
        if ev.two_digit_year:
            notes.append(Note(
                QUESTION, "two_digit_year", ev.name,
                f"{ev.two_digit_year} date(s) carry a two-digit year; the "
                "century is inferred, not stated.",
                {"count": ev.two_digit_year},
            ))
        if ev.string_sentinels:
            notes.append(Note(
                OBSERVATION, "string_sentinels", ev.name,
                "Placeholder strings present; these become category values "
                "unless declared as missing.",
                {"values": dict(ev.string_sentinels)},
            ))
    return notes


def _column_notes(
    cols: list[columns_mod.ColumnProfile],
    explained: set[str] | None = None,
) -> list[Note]:
    notes: list[Note] = []
    explained = explained or set()
    for c in cols:
        for prop in c.observed_properties():
            if prop["property"] == "sigma_establishable" and not prop["holds"]:
                notes.append(Note(
                    LIMITATION, "sigma_not_establishable", c.name,
                    "Sigma-based outlier rules are not valid on this column: "
                    + prop["evidence"] + ".",
                    {"sigma_ratio": c.sigma_ratio, "non_null": c.non_null},
                ))
            elif prop["property"] == "numeric_sentinel":
                notes.append(Note(
                    QUESTION, "numeric_sentinel", c.name,
                    "A conventional missing-value code is present as a real "
                    "number: " + prop["evidence"] + ". Confirm whether it means "
                    "missing.",
                    {},
                ))
            elif prop["property"] == "percent_range":
                # Only worth interrupting for when the name agrees; otherwise it
                # stays an observed property on the column and nobody is asked.
                if columns_mod.PERCENTISH_NAME.search(c.name):
                    notes.append(Note(
                        QUESTION, "percent_range_candidate", c.name,
                        f"Named like a percentage and ranges "
                        f"[{c.min_value}, {c.max_value}]. Confirm the bounds are "
                        "0-100 and bound checks become available.",
                        {"min": columns_mod._jsonable(c.min_value),
                         "max": columns_mod._jsonable(c.max_value)},
                    ))
            elif prop["property"] == "high_null" and c.name not in explained:
                notes.append(Note(
                    QUESTION, "high_null", c.name,
                    f"{c.null_fraction:.1%} of rows carry no value "
                    f"({c.non_null:,} of {c.rows:,} populated). Confirm whether "
                    "the column is expected to be sparse — if not, this is the "
                    "finding, and most checks on it are unavailable either way.",
                    {"null_fraction": round(c.null_fraction, 4),
                     "non_null": c.non_null},
                ))
            elif prop["property"] == "all_null":
                notes.append(Note(
                    LIMITATION, "all_null", c.name,
                    "Column carries no values; nothing can be checked on it.",
                    {},
                ))
    return notes


def build(path: Path, byte_cap: int = rawscan_mod.BYTE_CAP) -> Profile:
    prov = describe(path)
    reader = reader_for(path)

    # The raw pass runs FIRST and independently -- once DuckDB types a column it
    # has already committed to a separator and a date convention, and the
    # evidence for whether that commitment was right is gone.
    scan = rawscan_mod.scan(path, reader, byte_cap=byte_cap)

    con = duckdb.connect(":memory:")
    try:
        source = f"{reader}(?)"
        params = [str(path)]
        schema = [(c.name, c.declared_type) for c in prov.columns]
        cols = columns_mod.profile_columns(con, source, params, schema, prov.row_count)
        block_notes, explained = _comissing_notes(con, source, params, cols)
    finally:
        con.close()

    notes = _raw_notes(scan) + block_notes + _column_notes(cols, explained)

    if (scan.applicable and not scan.truncated and not scan.ragged_rows
            and scan.data_rows != prov.row_count):
        notes.append(Note(
            QUESTION, "row_count_mismatch", None,
            f"The raw pass counted {scan.data_rows:,} data rows but the "
            f"parser returned {prov.row_count:,}. The two disagree about "
            "where rows begin and end.",
            {"raw_rows": scan.data_rows, "parsed_rows": prov.row_count},
        ))

    return Profile(provenance=prov, rawscan=scan, columns=cols, notes=notes)
