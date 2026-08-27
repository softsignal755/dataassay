"""Command-line entry point.

Kept deliberately thin: argparse wiring and rendering only. Anything that
decides something belongs in a module the server can import without a CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dataassay import __version__
from dataassay.profile import LIMITATION, OBSERVATION, QUESTION, Profile, build
from dataassay.provenance import UnsupportedFormat

_SEVERITY_LABEL = {
    QUESTION: "Needs your answer",
    LIMITATION: "Cannot be checked",
    OBSERVATION: "Noted",
}


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _wrap(text: str, width: int, indent: str) -> list[str]:
    words, lines, current = text.split(), [], indent
    for w in words:
        if len(current) + len(w) + 1 > width and current.strip():
            lines.append(current)
            current = indent + w
        else:
            current = f"{current} {w}" if current.strip() else current + w
    if current.strip():
        lines.append(current)
    return lines


def _fmt(v, places: int = 4) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return f"{int(v):,}"
        return f"{v:,.{places}g}"
    if isinstance(v, int):
        return f"{v:,}"
    s = str(v)
    return s if len(s) <= 22 else s[:21] + "…"


def _render(profile: Profile, width: int = 88) -> str:
    p, scan = profile.provenance, profile.rawscan
    out = [
        f"{p.filename}  ({_human_bytes(p.size_bytes)}, via {p.reader})",
        f"  sha256      {p.content_sha256[:16]}…",
        f"  rows        {p.row_count:,}   columns  {p.column_count}",
        f"  audited_at  {p.audited_at}",
        f"  tool        {p.tool} {p.tool_version}",
    ]
    if scan.applicable:
        out.append(
            f"  raw pass    delimiter {scan.delimiter!r} "
            f"({scan.delimiter_confidence:.0%} of lines), {scan.encoding}"
        )
    out.append("")

    # Columns ------------------------------------------------------------------
    name_w = max((len(c.name) for c in profile.columns), default=4)
    name_w = min(max(name_w, 6), 28)
    out.append(
        f"  {'COLUMN':<{name_w}}  {'TYPE':<10} {'NULL%':>7} {'DISTINCT':>10} "
        f"{'MIN':>14} {'MEDIAN':>14} {'MAX':>14}"
    )
    out.append("  " + "─" * (name_w + 74))
    for c in profile.columns:
        median = c.quantiles.get("0.5") if c.kind == "numeric" else None
        out.append(
            f"  {c.name[:name_w]:<{name_w}}  {c.declared_type[:10]:<10} "
            f"{c.null_fraction:>6.1%} {c.distinct:>10,} "
            f"{_fmt(c.min_value):>14} {_fmt(median):>14} {_fmt(c.max_value):>14}"
        )
    out.append("")

    # Notes --------------------------------------------------------------------
    for severity in (QUESTION, LIMITATION, OBSERVATION):
        group = [n for n in profile.notes if n.severity == severity]
        if not group:
            continue
        out.append(f"  {_SEVERITY_LABEL[severity].upper()}  ({len(group)})")
        for n in group:
            where = f"{n.column}: " if n.column else ""
            out += _wrap(f"• {where}{n.message}", width, "    ")
        out.append("")

    questions = len(profile.questions)
    out.append(
        f"  {len(profile.columns)} columns profiled · {questions} open question"
        f"{'' if questions == 1 else 's'} · checks land in v0.2"
    )
    return "\n".join(out)


def _cmd_profile(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"assay: no such file: {path}", file=sys.stderr)
        return 2
    try:
        profile = build(path)
    except UnsupportedFormat as exc:
        print(f"assay: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(profile.to_dict(), indent=2, default=str))
    else:
        print(_render(profile))

    # Open questions are not failures -- the profile is still complete and
    # useful without answers. Exit 0 unless asked to treat them as blocking.
    if args.fail_on_question and profile.questions:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assay",
        description="Audit a tabular dataset. Nothing leaves your machine.",
    )
    parser.add_argument("--version", action="version", version=f"dataassay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    profile = sub.add_parser(
        "profile",
        help="characterize a file: provenance, raw-text evidence, measurements",
        description=(
            "Characterizes a dataset without checking it. Reports what could not "
            "be established as plainly as what could."
        ),
    )
    profile.add_argument("path", help="CSV or Parquet file")
    profile.add_argument("--json", action="store_true", help="emit the machine contract")
    profile.add_argument(
        "--fail-on-question",
        action="store_true",
        help="exit 1 if anything needs a human answer (for pipelines)",
    )
    profile.set_defaults(func=_cmd_profile)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
