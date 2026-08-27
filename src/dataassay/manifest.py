"""The interview's product: a declarative record of what this dataset is.

The conversation is only the most convenient way to author this file the first
time. What matters is the artifact, because it is what lets the same audit run
again next month without asking anything, and what lets it run at all somewhere
there is nobody to ask -- a pipeline, a server, CI.

Two sections, and the split is the whole design:

  detected   what the tool worked out on its own, written for reference. It is
             regenerated on every `assay init` and nothing is lost by editing
             over it.
  proposed   what a model suggested during the interview. Never applied. It
             sits beside `declared` so a person can read it, agree with it, and
             move it across -- which is the whole point: `declared` means a
             HUMAN said so, and an LLM writing directly into it would quietly
             destroy the one guarantee the manifest offers.
  declared   what a person says is true. Wins over both, always.

Keeping them apart means a value's provenance is never ambiguous. There is no
guessing later whether `time_axis: date` was inferred or confirmed -- the
section it sits in says so, and the report can be honest about which checks
rest on an assumption and which rest on an answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dataassay import __version__

MANIFEST_VERSION = 1
SUFFIX = ".assay.json"

_NOTE = (
    "Edit the 'declared' block. Anything you put there overrides 'detected', "
    "which is regenerated each time this file is written and is only there for "
    "reference. 'proposed' holds suggestions from `assay interview` and is "
    "never applied — move what you agree with into 'declared' yourself, because "
    "'declared' is the record that a person decided, and that is only worth "
    "something if it stays true. Every field is optional: what you leave out "
    "stays inferred, and the audit reports which checks that costs. List a "
    "question's code under 'skipped' to say you have seen it and are choosing "
    "not to answer — that is recorded as a deliberate gap rather than an unread "
    "one."
)


def path_for(data_file: Path) -> Path:
    return data_file.with_name(data_file.name + SUFFIX)


@dataclass
class Manifest:
    detected: dict[str, Any] = field(default_factory=dict)
    proposed: dict[str, Any] = field(default_factory=dict)
    declared: dict[str, Any] = field(default_factory=dict)
    schema_columns: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    source_path: Path | None = None

    # -- reading ---------------------------------------------------------------

    def get(self, key: str) -> tuple[Any, str]:
        """(value, provenance) -- 'declared', 'detected', or 'unset'."""
        if key in self.declared and self.declared[key] not in (None, [], ""):
            return self.declared[key], "declared"
        if key in self.detected and self.detected[key] not in (None, [], ""):
            return self.detected[key], "detected"
        return None, "unset"

    def declared_value(self, key: str) -> Any:
        value, provenance = self.get(key)
        return value if provenance == "declared" else None

    # -- writing ---------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "manifest_version": MANIFEST_VERSION,
            "written_by": f"dataassay {__version__}",
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": _NOTE,
            "schema": {"columns": self.schema_columns},
            "declared": self.declared,
            "proposed": self.proposed,
            "skipped": self.skipped,
            "questions": self.questions,
            "detected": self.detected,
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")


def load(path: Path) -> Manifest:
    raw = json.loads(path.read_text())
    version = raw.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise ValueError(
            f"{path.name}: manifest_version {version!r}, expected {MANIFEST_VERSION}"
        )
    return Manifest(
        detected=raw.get("detected") or {},
        declared=raw.get("declared") or {},
        proposed=raw.get("proposed") or {},
        schema_columns=(raw.get("schema") or {}).get("columns") or [],
        skipped=raw.get("skipped") or [],
        questions=raw.get("questions") or [],
        source_path=path,
    )


def discover(data_file: Path, explicit: Path | None = None) -> Manifest | None:
    """Find the manifest for a data file, if one exists."""
    if explicit is not None:
        return load(explicit)
    candidate = path_for(data_file)
    return load(candidate) if candidate.is_file() else None


def from_audit(profile, structure) -> Manifest:
    """Build a manifest from what an audit worked out, ready to be edited.

    The `declared` block is left EMPTY rather than pre-filled with the detected
    values. Pre-filling would silently promote every guess to an answer the
    moment someone saved the file -- which is exactly the failure this split
    exists to prevent.
    """
    return Manifest(
        schema_columns=[c.name for c in profile.columns],
        detected={
            "time_axis": structure.time_axis,
            "group_columns": structure.group_columns,
            "grain": structure.grain,
            "grain_is_unique": structure.grain_is_unique,
        },
        declared={},
        skipped=[],
        questions=[
            {"code": n.code, "column": n.column, "question": n.message}
            for n in profile.questions
        ],
    )


def schema_drift(manifest: Manifest, columns: list[str]) -> tuple[list[str], list[str]]:
    """(added, removed) relative to what the manifest was written against."""
    if not manifest.schema_columns:
        return [], []
    known = set(manifest.schema_columns)
    actual = set(columns)
    return sorted(actual - known), sorted(known - actual)
