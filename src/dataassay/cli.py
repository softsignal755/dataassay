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
from dataassay.provenance import UnsupportedFormat, describe


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _render_text(prov) -> str:
    lines = [
        f"{prov.filename}  ({_human_bytes(prov.size_bytes)}, via {prov.reader})",
        f"  sha256      {prov.content_sha256[:16]}…",
        f"  rows        {prov.row_count:,}",
        f"  columns     {prov.column_count}",
        f"  audited_at  {prov.audited_at}",
        f"  tool        {prov.tool} {prov.tool_version}",
        "",
    ]
    width = max((len(c.name) for c in prov.columns), default=0)
    lines += [f"  {c.name:<{width}}  {c.declared_type}" for c in prov.columns]
    return "\n".join(lines)


def _cmd_profile(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"assay: no such file: {path}", file=sys.stderr)
        return 2
    try:
        prov = describe(path)
    except UnsupportedFormat as exc:
        print(f"assay: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"provenance": prov.to_dict()}, indent=2))
    else:
        print(_render_text(prov))
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
        help="report a file's provenance and declared shape",
        description=(
            "Phase 0 slice: provenance header and declared column types. "
            "Property detection and checks land in later versions."
        ),
    )
    profile.add_argument("path", help="CSV or Parquet file")
    profile.add_argument("--json", action="store_true", help="emit JSON instead of text")
    profile.set_defaults(func=_cmd_profile)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
