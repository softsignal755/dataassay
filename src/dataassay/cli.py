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
from dataassay import audit as audit_mod
from dataassay import manifest as manifest_mod
from dataassay.checks.base import DEFECT, SUSPECT
from dataassay.checks.registry import CATALOG_VERSION, catalog_dict
from dataassay.profile import LIMITATION, OBSERVATION, QUESTION, Profile, build
from dataassay.provenance import UnsupportedFormat

_SEVERITY_LABEL = {
    QUESTION: "Needs your answer",
    LIMITATION: "Cannot be checked",
    OBSERVATION: "Noted",
}

_DISPOSITION_LABEL = {
    DEFECT: "Likely defects",
    SUSPECT: "Worth a look",
    "bookkeeping": "The source doing its own bookkeeping",
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



def _render_audit(a: audit_mod.Audit, width: int = 88) -> str:
    p, s = a.profile.provenance, a.structure
    out = [
        f"{p.filename}  ({_human_bytes(p.size_bytes)}, via {p.reader})",
        f"  sha256      {p.content_sha256[:16]}…",
        f"  rows        {p.row_count:,}   columns  {p.column_count}",
        f"  catalog     {a.catalog_version}",
    ]
    if a.manifest_path:
        out.append(f"  manifest    {Path(a.manifest_path).name}")
    if s.time_axis:
        out.append(f"  time axis   {s.time_axis}  ({s.time_axis_basis})")
    if s.grain:
        unique = "unique" if s.grain_is_unique else (
            f"NOT unique — {s.duplicate_grain_rows:,} excess row(s)"
        )
        how = "declared" if s.grain_declared else "inferred"
        out.append(f"  grain       {' × '.join(s.grain)}  ({how}, {unique})")
    out.append("")

    if a.findings:
        for disposition in (DEFECT, SUSPECT, "bookkeeping"):
            group = [f for f in a.findings if f.disposition == disposition]
            if not group:
                continue
            out.append(f"  {_DISPOSITION_LABEL[disposition].upper()}  ({len(group)})")
            for f in group:
                where = f"{f.column}: " if f.column else ""
                out += _wrap(f"• {where}{f.summary}", width, "    ")
                out += _wrap(
                    f"[{f.confidence.level}] " + "; ".join(f.confidence.inputs),
                    width, "      ",
                )
                out.append("")
    else:
        out += ["  No findings.", ""]

    cov = a.coverage
    out.append(
        f"  COVERAGE  {len(cov.ran)} of {cov.total} checks ran"
        + (f", {len(cov.withheld)} withheld" if cov.withheld else "")
        + (f", {len(cov.blocked)} waiting on an answer" if cov.blocked else "")
    )
    for cid, reason in cov.withheld:
        out += _wrap(f"– {cid}: {reason}", width, "    ")
    for cid, question in cov.blocked:
        out += _wrap(f"? {cid}: {question}", width, "    ")

    if a.profile.questions:
        out.append("")
        out.append(f"  PROFILE QUESTIONS  ({len(a.profile.questions)})")
        for n in a.profile.questions:
            where = f"{n.column}: " if n.column else ""
            out += _wrap(f"• {where}{n.message}", width, "    ")

    for note in s.assumptions:
        out += _wrap(f"~ {note}", width, "    ")
    return "\n".join(out)


def _cmd_audit(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"assay: no such file: {path}", file=sys.stderr)
        return 2
    try:
        result = audit_mod.run(
            path,
            manifest_path=Path(args.manifest) if args.manifest else None,
            use_manifest=not args.no_manifest,
        )
    except UnsupportedFormat as exc:
        print(f"assay: {exc}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"assay: manifest: {exc}", file=sys.stderr)
        return 2

    if args.report:
        from dataassay import report as report_mod

        html_path, csv_path = report_mod.build(result, path, Path(args.report))
        print(f"Wrote {html_path}")
        print(f"Wrote {csv_path}")
    elif args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(_render_audit(result))

    if args.fail_on_finding and any(f.disposition == DEFECT for f in result.findings):
        return 1
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"assay: no such file: {path}", file=sys.stderr)
        return 2
    target = Path(args.output) if args.output else manifest_mod.path_for(path)
    if target.exists() and not args.force:
        print(
            f"assay: {target.name} already exists. Editing it is the point — "
            "pass --force only if you mean to discard what is in it.",
            file=sys.stderr,
        )
        return 2

    try:
        result = audit_mod.run(path, use_manifest=False)
    except UnsupportedFormat as exc:
        print(f"assay: {exc}", file=sys.stderr)
        return 2

    m = manifest_mod.from_audit(result.profile, result.structure)
    m.write(target)
    print(f"Wrote {target}")
    print(f"  {len(m.schema_columns)} column(s) recorded")
    if m.detected.get("time_axis"):
        print(f"  detected time axis: {m.detected['time_axis']}")
    if m.detected.get("grain"):
        print(
            f"  detected grain: {' × '.join(m.detected['grain'])}"
            + ("" if m.detected.get("grain_is_unique") else "  (NOT unique)")
        )
    if m.questions:
        print(f"  {len(m.questions)} open question(s) recorded for you to answer")
    print("\nEdit the 'declared' block to answer them. Declared values override "
          "detected ones, and\nthe next audit will use them without asking.")
    return 0


def _cmd_interview(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"assay: no such file: {path}", file=sys.stderr)
        return 2

    from dataassay.llm import interview as interview_mod
    from dataassay.llm import payload as payload_mod
    from dataassay.llm import provider as provider_mod

    try:
        result = audit_mod.run(path)
    except UnsupportedFormat as exc:
        print(f"assay: {exc}", file=sys.stderr)
        return 2

    body = payload_mod.build(result, redact_values=args.redact_values)

    if args.show_payload:
        print(json.dumps(body, indent=2, default=str))
        return 0

    print(payload_mod.summarize(body))
    print()
    if not args.yes:
        try:
            answer = input("Send this to Anthropic? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Nothing was sent.")
            return 0

    try:
        provider = provider_mod.get(args.provider)
        if args.model:
            provider.model = args.model
        outcome = interview_mod.run(body, provider, context=args.context)
    except provider_mod.MissingCredentials as exc:
        print(f"assay: {exc}", file=sys.stderr)
        return 2
    except provider_mod.ProviderError as exc:
        print(f"assay: {exc}", file=sys.stderr)
        return 1

    names = [c.name for c in result.profile.columns]
    kept, dropped = interview_mod.validate(outcome.declarations, names)

    target = Path(args.output) if args.output else manifest_mod.path_for(path)
    existing = manifest_mod.load(target) if target.is_file() else None
    m = manifest_mod.from_audit(result.profile, result.structure)
    if existing:
        m.declared = existing.declared
        m.skipped = existing.skipped
    m.proposed = kept
    m.questions = [
        {
            "code": "interview",
            "column": q.get("column"),
            "question": q.get("ask", ""),
            "proposed_answer": q.get("proposed_answer"),
            "why_it_matters": q.get("why_it_matters", ""),
        }
        for q in outcome.questions
    ]
    m.write(target)

    print()
    if outcome.reasoning:
        print("\n".join(_wrap(outcome.reasoning, 88, "")))
        print()
    if kept:
        print(f"  PROPOSED  ({len(kept)})  — written to {target.name} under "
              "'proposed', NOT applied")
        for key, value in kept.items():
            shown = " × ".join(value) if isinstance(value, list) else value
            print(f"    • {key}: {shown}")
        print()
        print("  Move what you agree with into 'declared' yourself. 'declared' "
              "records that a")
        print("  person decided, and that is only worth something if it stays "
              "true.")
    if dropped:
        print()
        print(f"  DISCARDED  ({len(dropped)})  — named columns this file "
              "does not have")
        for d in dropped:
            print(f"    • {d}")
    if outcome.questions:
        print()
        print(f"  WORTH YOUR ANSWER  ({len(outcome.questions)})")
        for q in outcome.questions:
            where = f"{q['column']}: " if q.get("column") else ""
            print("\n".join(_wrap(f"• {where}{q.get('ask', '')}", 88, "    ")))
            if q.get("proposed_answer"):
                print("\n".join(_wrap(
                    f"likely: {q['proposed_answer']}", 88, "      ")))
            if q.get("why_it_matters"):
                print("\n".join(_wrap(q["why_it_matters"], 88, "      ")))
    if outcome.unresolved:
        print()
        print(f"  LEFT UNRESOLVED  ({len(outcome.unresolved)})")
        for u in outcome.unresolved:
            print("\n".join(_wrap(f"• {u}", 88, "    ")))

    if outcome.input_tokens is not None:
        print()
        print(f"  {outcome.model} · {outcome.input_tokens:,} in / "
              f"{outcome.output_tokens:,} out")
    return 0


def _cmd_catalog(args: argparse.Namespace) -> int:
    cat = catalog_dict()
    if args.json:
        print(json.dumps(cat, indent=2))
        return 0
    print(f"dataassay check catalog {cat['catalog_version']}\n")
    for spec in cat["checks"]:
        print(f"  {spec['id']}  —  {spec['name']}")
        for label, key in (("detects", "detects"), ("gate", "gate"),
                           ("not the obvious check", "not_the_obvious"),
                           ("earned by", "traces_to")):
            if spec[key]:
                for para in spec[key].split("\n\n"):
                    print("\n".join(_wrap(f"{label}: {para}", 88, "      ")))
                    label = " " * len(label)
        print()
    return 0


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

    audit = sub.add_parser(
        "audit",
        help="characterize, then run every check the properties make valid",
        description=(
            "Profiles the file, infers its structure, runs only the checks its "
            "established properties make valid, and reports what was withheld "
            "alongside what was found."
        ),
    )
    audit.add_argument("path", help="CSV or Parquet file")
    audit.add_argument("--json", action="store_true", help="emit the machine contract")
    audit.add_argument(
        "--manifest", metavar="PATH",
        help="use this manifest instead of looking for <file>.assay.json",
    )
    audit.add_argument(
        "--no-manifest", action="store_true",
        help="ignore any manifest and infer everything",
    )
    audit.add_argument(
        "--report", metavar="OUT.html",
        help="write a self-contained HTML report and a flagged-items CSV",
    )
    audit.add_argument(
        "--fail-on-finding",
        action="store_true",
        help="exit 1 if any likely defect was found (for pipelines)",
    )
    audit.set_defaults(func=_cmd_audit)

    init = sub.add_parser(
        "init",
        help="write a manifest recording what this dataset is",
        description=(
            "Writes <file>.assay.json: what the tool detected, the questions it "
            "could not answer, and an empty 'declared' block for you to fill in. "
            "Declared values override detected ones, so the next audit — and "
            "every audit after it, anywhere, with nobody present — uses your "
            "answers instead of guessing."
        ),
    )
    init.add_argument("path", help="CSV or Parquet file")
    init.add_argument("-o", "--output", metavar="PATH", help="write here instead")
    init.add_argument(
        "--force", action="store_true", help="overwrite an existing manifest"
    )
    init.set_defaults(func=_cmd_init)

    interview = sub.add_parser(
        "interview",
        help="ask a model to characterize this dataset (sends metadata only)",
        description=(
            "Sends the PROFILE — column names, types, counts, quantiles, and "
            "the questions the profiler could not answer — to a model, and "
            "writes what it proposes into the manifest's 'proposed' block. "
            "Never a row of your data, and never applied without you moving it "
            "into 'declared' yourself. You see exactly what would be sent "
            "before it is sent. Requires: pip install 'dataassay[llm]'"
        ),
    )
    interview.add_argument("path", help="CSV or Parquet file")
    interview.add_argument(
        "--show-payload", action="store_true",
        help="print exactly what would be sent, send nothing, exit",
    )
    interview.add_argument(
        "--redact-values", action="store_true",
        help="strip the distinct values of low-cardinality columns",
    )
    interview.add_argument(
        "--context", metavar="TEXT",
        help="one line on what this data is and where it came from",
    )
    interview.add_argument("--yes", action="store_true", help="skip the confirmation")
    interview.add_argument("--provider", default="anthropic")
    interview.add_argument("--model", help="override the model")
    interview.add_argument("-o", "--output", metavar="PATH", help="manifest to write")
    interview.set_defaults(func=_cmd_interview)

    catalog = sub.add_parser(
        "catalog",
        help="print the check catalog",
        description=(
            f"The checks this version knows about (catalog {CATALOG_VERSION}). "
            "Each entry records what it detects, what property gates it, and "
            "where a more obvious detector fails."
        ),
    )
    catalog.add_argument("--json", action="store_true")
    catalog.set_defaults(func=_cmd_catalog)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
