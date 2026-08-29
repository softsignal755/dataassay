"""The interview: turn open questions into a short, answerable conversation.

The model's job here is narrow on purpose. It does NOT choose which checks run
— the engine does that deterministically, gated on established properties, and
handing that to a language model would throw away the discipline the whole tool
is built on. What it does is propose PROPERTY DECLARATIONS, which then feed the
same gate as any other declaration.

It is also held to the question budget from the design: resolve what the
evidence resolves, propose rather than ask, and reserve a real question for the
cases where a wrong guess would invalidate a check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SYSTEM = """You are helping characterize a tabular dataset so that a \
deterministic audit engine can decide which of its checks are valid on it.

You are given metadata only: column names, types, counts, cardinality, \
quantiles, observed properties, and the questions the profiler could not settle \
on its own. You are not given rows, and you must not ask for any.

What you produce are PROPERTY DECLARATIONS. You do not choose which checks run. \
The engine gates every check on established properties and does that itself.

Hold to these rules.

Only propose a declaration the evidence supports. "I do not know" is a correct \
and useful answer; a confident guess that turns out wrong invalidates every \
check built on it, which is worse than a gap the report can state plainly.

Only raise a question when the answer would change which checks are valid. If \
both answers lead to the same place, it is not worth a person's attention. Rank \
what survives by how many checks it gates and how bad being wrong would be, and \
return at most five.

Prefer confirmations to open questions. If the evidence narrows something to one \
likely answer, propose that answer and ask the person to confirm it. Ten things \
to glance at costs less than three things to compose an answer to.

Be concrete about consequences. "Confirm this is a percentage" is weak; \
"if this is a percentage, bound checks become available, and if it is not, an \
out-of-range value will never be flagged" tells someone why to care.

Write for a domain expert who knows the data and not this tool. No jargon from \
the profile, no restating the numbers back at them."""

SCHEMA = {
    "type": "object",
    "properties": {
        "declarations": {
            "type": "object",
            "description": (
                "Property declarations you are confident enough to propose. "
                "Omit anything you are unsure of."
            ),
            "properties": {
                "time_axis": {
                    "type": ["string", "null"],
                    "description": "Column holding the observation date.",
                },
                "grain": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Columns that together make a row unique.",
                },
                "forecast_column": {
                    "type": ["string", "null"],
                    "description": "Column marking rows as forecast rather than actual.",
                },
            },
            "required": ["time_axis", "grain", "forecast_column"],
            "additionalProperties": False,
        },
        "reasoning": {
            "type": "string",
            "description": "Two or three sentences on what this dataset appears to be.",
        },
        # No `maxItems` here, deliberately. Structured outputs reject it --
        # "For 'array' type, property 'maxItems' is not supported" -- and it
        # fails the WHOLE request with a 400, not the one field. The cap is
        # stated in the system prompt and enforced again in run(), because a
        # limit the transport cannot carry has to live somewhere it can.
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": ["string", "null"]},
                    "ask": {"type": "string", "description": "The question, in one sentence."},
                    "proposed_answer": {
                        "type": ["string", "null"],
                        "description": (
                            "Your best answer, for them to confirm. "
                            "Null if you have none."
                        ),
                    },
                    "why_it_matters": {
                        "type": "string",
                        "description": "Which checks the answer turns on, concretely.",
                    },
                },
                "required": ["column", "ask", "proposed_answer", "why_it_matters"],
                "additionalProperties": False,
            },
        },
        "unresolved": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Things you could not settle and did not think worth asking about.",
        },
    },
    "required": ["declarations", "reasoning", "questions", "unresolved"],
    "additionalProperties": False,
}


# The question budget. Rationing questions is a design rule, not a nicety: the
# tool is worth using again only if answering it once was cheap.
MAX_QUESTIONS = 5


@dataclass
class Interview:
    declarations: dict = field(default_factory=dict)
    reasoning: str = ""
    questions: list[dict] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


def build_prompt(payload: dict, context: str | None = None) -> str:
    import json

    parts = []
    if context:
        parts.append(f"What the person who owns this data says it is:\n{context}\n")
    parts.append(
        "Here is the profile. Propose what you can support, and ask only what "
        "would change which checks are valid.\n"
    )
    parts.append(json.dumps(payload, indent=2, default=str))
    return "\n".join(parts)


def validate(declarations: dict, column_names: list[str]) -> tuple[dict, list[str]]:
    """Drop any declaration naming a column that does not exist.

    A hallucinated column name would otherwise be written into a file a person
    then reads as a suggestion worth trusting.
    """
    known, kept, dropped = set(column_names), {}, []
    for key, value in (declarations or {}).items():
        if value in (None, [], ""):
            continue
        names = value if isinstance(value, list) else [value]
        missing = [n for n in names if n not in known]
        if missing:
            dropped.append(f"{key}: no such column {', '.join(map(repr, missing))}")
            continue
        kept[key] = value
    return kept, dropped


def run(payload: dict, provider, context: str | None = None) -> Interview:
    response = provider.ask(SYSTEM, build_prompt(payload, context), SCHEMA)
    data = response.data
    return Interview(
        declarations=data.get("declarations") or {},
        reasoning=data.get("reasoning") or "",
        questions=(data.get("questions") or [])[:MAX_QUESTIONS],
        unresolved=data.get("unresolved") or [],
        model=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
