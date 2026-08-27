"""Checks that need no ordering: shape, encoding, and identity."""

from __future__ import annotations

from dataassay.checks.base import (
    DEFECT,
    HIGH,
    MEDIUM,
    SUSPECT,
    Applicability,
    CheckContext,
    CheckSpec,
    Confidence,
    Finding,
)
from dataassay.columns import q

# UTF-8 read as latin-1 leaves a signature: a lead byte surfacing as A-tilde or
# A-circumflex followed by a continuation byte in the C1/supplement range. The
# PAIR is the tell -- matching A-tilde alone would flag every uppercase
# Portuguese place name ("SAO PAULO" spelled correctly) as corrupt.
_MOJIBAKE_RE = r"[\x{c3}\x{c2}][\x{80}-\x{bf}]|\x{e2}\x{20ac}|\x{ef}\x{bb}\x{bf}"


class ConstantNumericColumn:
    spec = CheckSpec(
        id="constant_numeric",
        name="Numeric column carries a single value",
        detects=(
            "A numeric column with one distinct value across the whole file, "
            "most dangerously all zeros."
        ),
        gate="A numeric column with at least one non-null value.",
        default_disposition=SUSPECT,
        not_the_obvious=(
            "The obvious check is for NULLs, and it finds nothing here. When a "
            "reader names a source column wrongly the value does not go "
            "missing -- it goes to zero, and zero is a legitimate number that "
            "every downstream type check, sum, and average accepts silently."
        ),
        traces_to=(
            "C2/C3: a single- vs double-underscore mismatch in a CFTC column "
            "name sent swap-shorts to zero, flipping a published headline "
            "signal's sign and understating another by ~40% weekly."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        if not ctx.columns("numeric"):
            return Applicability.no("no numeric columns")
        if ctx.profile.provenance.row_count < 2:
            return Applicability.no("fewer than 2 rows: constancy is meaningless")
        return Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        out = []
        for col in ctx.columns("numeric"):
            if col.non_null == 0 or col.distinct != 1:
                continue
            all_zero = col.zeros == col.non_null
            value = col.min_value
            # Negative zero does not come from a source file; it comes from
            # arithmetic. Weak evidence, but it points at a computed column
            # rather than a reported one.
            negative_zero = all_zero and str(value).startswith("-")
            inputs = [f"{col.non_null:,} non-null rows, 1 distinct value"]
            if all_zero:
                inputs.append(
                    "zero is both the signature of a column that was renamed, "
                    "misspelled, or never joined AND a legitimate value for a "
                    "quantity that is simply absent everywhere in scope — "
                    "nothing in the data distinguishes them"
                )
            if negative_zero:
                inputs.append(
                    "the value is negative zero, which arithmetic produces and "
                    "a source file does not — this column is computed"
                )
            out.append(Finding(
                check_id=self.spec.id,
                column=col.name,
                disposition=SUSPECT,
                summary=(
                    f"Every one of {col.non_null:,} values is {value}"
                    + (". Either the source column never arrived, or this "
                       "quantity is genuinely absent everywhere in this "
                       "dataset. Which one is a question only you can settle, "
                       "and the answer decides whether the column is a bug or "
                       "just dead weight."
                       if all_zero else
                       ". Confirm the column is genuinely constant.")
                ),
                evidence={"distinct": 1, "value": str(value),
                          "non_null": col.non_null, "all_zero": all_zero,
                          "negative_zero": negative_zero},
                predicate=(
                    f"SELECT count(DISTINCT {q(col.name)}), count({q(col.name)}) "
                    f"FROM <source>"
                ),
                confidence=Confidence(MEDIUM, inputs),
            ))
        return out


class Mojibake:
    spec = CheckSpec(
        id="mojibake",
        name="Text decoded with the wrong encoding",
        detects="UTF-8 bytes that were read as latin-1, corrupting accented text.",
        gate="At least one text column.",
        default_disposition=DEFECT,
        not_the_obvious=(
            "Checking that the file decodes as UTF-8 does not catch this. The "
            "corruption happened UPSTREAM: the bytes now on disk are valid "
            "UTF-8 encoding the wrong characters. Only the resulting character "
            "pairs give it away."
        ),
        traces_to=(
            "C5: a fetcher wrote UTF-8 and the report read latin-1, so an "
            "ordinal indicator mojibaked and every safra filter matched "
            "nothing. The corn section rendered empty and the report's anchor "
            "divergence vanished — while still publishing with that day's date."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        if not ctx.columns("text"):
            return Applicability.no("no text columns")
        return Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        out = []
        for col in ctx.columns("text"):
            if col.non_null == 0:
                continue
            predicate = (
                f"SELECT count(*) FROM <source> "
                f"WHERE regexp_matches({q(col.name)}, '{_MOJIBAKE_RE}')"
            )
            (hits,) = ctx.fetch(
                f"SELECT count(*) FROM {ctx.source} "
                f"WHERE regexp_matches({q(col.name)}, '{_MOJIBAKE_RE}')"
            )[0]
            if not hits:
                continue
            examples = [
                r[0] for r in ctx.fetch(
                    f"SELECT DISTINCT {q(col.name)} FROM {ctx.source} "
                    f"WHERE regexp_matches({q(col.name)}, '{_MOJIBAKE_RE}') LIMIT 5"
                )
            ]
            out.append(Finding(
                check_id=self.spec.id,
                column=col.name,
                disposition=DEFECT,
                summary=(
                    f"{hits:,} value(s) carry the signature of UTF-8 text read "
                    f"as latin-1, e.g. {examples[0]!r}. Any filter or join on "
                    "this column will silently match nothing for the affected "
                    "rows."
                ),
                evidence={"rows": hits, "examples": examples},
                predicate=predicate,
                confidence=Confidence(HIGH, [
                    f"{hits:,} rows match a lead/continuation byte pair",
                    "the pair is matched, not the lead character alone, so "
                    "correctly-spelled accented text does not trigger it",
                ]),
                raw_values=True,
            ))
        return out


class DuplicateRows:
    spec = CheckSpec(
        id="duplicate_rows",
        name="Byte-identical rows",
        detects="Rows that repeat every column of another row.",
        gate="At least two rows. Needs no key, so it is always available.",
        default_disposition=DEFECT,
        not_the_obvious=(
            "This is the half of duplicate detection that needs no knowledge of "
            "the table. Two rows identical in every column cannot be two "
            "observations, whatever the key turns out to be — which is why it "
            "runs when the grain check cannot."
        ),
        traces_to="The repeated-load class: a merge that ran twice.",
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        if ctx.profile.provenance.row_count < 2:
            return Applicability.no("fewer than 2 rows")
        return Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        # Rows minus distinct rows. GROUP BY ALL looks like the natural way to
        # write this and is a trap: with only an aggregate in the select list it
        # groups by nothing, and every file reads as one enormous duplicate.
        (excess,) = ctx.fetch(
            f"WITH s AS (SELECT * FROM {ctx.source}) "
            f"SELECT (SELECT count(*) FROM s) "
            f"     - (SELECT count(*) FROM (SELECT DISTINCT * FROM s))"
        )[0]
        if not excess:
            return []
        return [Finding(
            check_id=self.spec.id,
            column=None,
            disposition=DEFECT,
            summary=(
                f"{excess:,} row(s) repeat another row exactly, in every column. "
                "Two observations cannot be identical in every field, so these "
                "are the same record written more than once."
            ),
            evidence={"excess_rows": excess,
                      "of_total": ctx.profile.provenance.row_count},
            predicate=(
                "SELECT count(*) - (SELECT count(*) FROM "
                "(SELECT DISTINCT * FROM <source>)) FROM <source>"
            ),
            confidence=Confidence(HIGH, [
                f"{excess:,} of {ctx.profile.provenance.row_count:,} rows are "
                "exact repeats",
                "needs no assumption about the key",
            ]),
        )]


class DuplicateGrain:
    spec = CheckSpec(
        id="duplicate_grain",
        name="More than one row per key",
        detects="Rows that repeat the inferred grain of the table.",
        gate="A grain could be inferred or was declared.",
        default_disposition=SUSPECT,
        not_the_obvious=(
            "Looking for exact duplicate ROWS is the obvious version and it "
            "misses the dangerous case. Two rows sharing a key but disagreeing "
            "on values are not duplicates -- they are a conflict, and whichever "
            "one a last-wins merge keeps is arbitrary."
        ),
        traces_to=(
            "C1: a substring match pulled a calendar-spread-options contract in "
            "alongside the real one; last-wins dedupe replaced the true row on "
            "131 dates, including the week that was on the live page."
        ),
    )

    def applies(self, ctx: CheckContext) -> Applicability:
        s = ctx.structure
        if s.grain_is_unique is None:
            return Applicability.needs_answer(
                "No key or time axis could be inferred, so there is nothing to "
                "call a duplicate. What combination of columns should make a "
                "row unique?"
            )
        if s.candidates_exhausted:
            # We did not find the key. Saying "you have duplicates" here would
            # be reporting our own ignorance as the data's defect.
            return Applicability.needs_answer(
                "No combination of the low-cardinality columns makes a row "
                "unique, so the key is wider than anything that could be "
                "inferred (" + " × ".join(s.grain) + " is not enough). What "
                "makes a row unique here? Until that is known, duplicates "
                "cannot be told apart from a wide key."
            )
        return Applicability.yes()

    def run(self, ctx: CheckContext) -> list[Finding]:
        s = ctx.structure
        if s.grain_is_unique:
            return []

        keys = ", ".join(q(k) for k in s.grain)
        others = [
            c.name for c in ctx.profile.columns if c.name not in s.grain
        ]
        # The discriminator: do the colliding rows agree? Identical rows are a
        # merge that ran twice. Rows that disagree are a conflict, and something
        # downstream is silently choosing between them.
        conflicting = 0
        if others:
            value_cols = ", ".join(q(c) for c in others)
            (conflicting,) = ctx.fetch(
                f"SELECT count(*) FROM (SELECT {keys} FROM {ctx.source} "
                f"GROUP BY {keys} "
                f"HAVING count(*) > 1 AND count(DISTINCT ({value_cols})) > 1)"
            )[0]

        examples = ctx.fetch(
            f"SELECT {keys}, count(*) n FROM {ctx.source} GROUP BY {keys} "
            f"HAVING count(*) > 1 ORDER BY n DESC LIMIT 5"
        )
        disposition = DEFECT if conflicting else SUSPECT
        inputs = [f"grain {s.grain} inferred, not declared"]
        if conflicting:
            inputs.append(
                f"{conflicting:,} key(s) carry rows that DISAGREE on their "
                "values — a conflict, not a repeat"
            )
        else:
            inputs.append("colliding rows are identical — a repeated load, not a conflict")

        return [Finding(
            check_id=self.spec.id,
            column=None,
            disposition=disposition,
            summary=(
                f"{s.duplicate_grain_rows:,} row(s) beyond one per "
                f"{' × '.join(s.grain)}."
                + (f" {conflicting:,} of those keys carry rows that disagree on "
                   "their values, so any merge is silently picking a winner."
                   if conflicting else
                   " The colliding rows are identical, which usually means a "
                   "load ran twice.")
            ),
            evidence={
                "grain": s.grain,
                "excess_rows": s.duplicate_grain_rows,
                "conflicting_keys": conflicting,
                "examples": [[str(v) for v in row] for row in examples],
            },
            predicate=(
                f"SELECT {keys}, count(*) FROM <source> GROUP BY {keys} "
                f"HAVING count(*) > 1"
            ),
            confidence=Confidence(HIGH if conflicting else MEDIUM, inputs),
            raw_values=True,
        )]
