"""What crosses the network, and nothing else.

The whole product claim is that your data stays on your machine. This module is
where that claim is either kept or quietly broken, so it is deliberately the
smallest, most boring file in the package: it takes an audit and returns a dict,
and there is exactly one function that decides what goes in it.

The rule is metadata only. Counts, types, quantiles, cardinality, the questions
the profiler could not answer. Never a row.

Two things are honest to admit rather than hide:

  Column NAMES go. They have to -- deciding that `mm_pct_oi` is a percentage is
  not possible without seeing `mm_pct_oi`. A schema is disclosure, and anyone
  who cannot disclose one should not use this feature.

  Low-cardinality TOP VALUES are real cell values. Characterizing a categorical
  column without seeing its categories is not possible either, so they are
  included by default, flagged in the payload, and strippable with one flag.

`summarize()` exists so a person can read what is about to be sent before it is
sent. Not a log line afterwards -- before.
"""

from __future__ import annotations

import json
from typing import Any


def build(audit, redact_values: bool = False) -> dict[str, Any]:
    """The metadata-only payload for an audit."""
    profile = audit.profile.to_dict()
    prov = profile["provenance"]

    columns = []
    for col in profile["columns"]:
        col = dict(col)
        if redact_values:
            col.pop("top_values", None)
            col.pop("top_values_are_raw_values", None)
        columns.append(col)

    return {
        "kind": "dataassay.profile",
        "schema_version": profile["schema_version"],
        # The local path is dropped: it says nothing useful about the data and
        # something about the machine.
        "file": {
            "name": prov["filename"],
            "rows": prov["row_count"],
            "columns": prov["column_count"],
            "reader": prov["reader"],
            "content_sha256": prov["content_sha256"],
        },
        "parsing": profile["rawscan"],
        "columns": columns,
        "open_questions": [
            n for n in profile["notes"] if n["severity"] == "question"
        ],
        "structure": audit.structure.to_dict(),
        "values_included": not redact_values,
    }


def summarize(payload: dict) -> str:
    """A plain-language account of what is in the payload, for a human to read
    BEFORE anything is sent."""
    cols = payload["columns"]
    with_values = [c["name"] for c in cols if c.get("top_values_are_raw_values")]
    size = len(json.dumps(payload))

    lines = [
        f"About to send {size:,} bytes describing {payload['file']['name']}:",
        "",
        f"  • the file's name, shape ({payload['file']['rows']:,} rows × "
        f"{payload['file']['columns']} columns) and content hash",
        f"  • {len(cols)} column names, with their types, null counts, "
        "cardinality, quantiles and observed properties",
        f"  • {len(payload['open_questions'])} question(s) the profiler could "
        "not answer on its own",
        "  • the inferred time axis and grain",
        "",
        "  NOT included: any row of your data.",
    ]
    if with_values:
        lines += [
            "",
            f"  One exception, and it is real: {len(with_values)} "
            "low-cardinality column(s) include their distinct values —",
            "    " + ", ".join(with_values[:8])
            + (" …" if len(with_values) > 8 else ""),
            "  Those are actual cell contents. A categorical column cannot be "
            "characterized without them.",
            "  Pass --redact-values to strip them; the interview gets weaker "
            "and nothing else changes.",
        ]
    else:
        lines += ["", "  Values redacted: no cell contents at all."]
    return "\n".join(lines)


def assert_no_rows(payload: dict, sample_values: list[str]) -> list[str]:
    """Belt and braces: given values known to live in the file, confirm none of
    them reached the payload except as declared categories."""
    blob = json.dumps(payload)
    declared = set()
    for col in payload["columns"]:
        for value, _ in col.get("top_values") or []:
            declared.add(str(value))
    return [v for v in sample_values if str(v) in blob and str(v) not in declared]
