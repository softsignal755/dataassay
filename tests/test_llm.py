"""The LLM adapter, and the boundary it is not allowed to cross."""

import ast
import json
from pathlib import Path

import pytest

from dataassay.audit import run
from dataassay.llm import interview as interview_mod
from dataassay.llm import payload as payload_mod
from dataassay.llm import provider as provider_mod

NETWORK_MODULES = {
    "anthropic", "httpx", "httpx2", "requests", "urllib", "urllib3",
    "socket", "http", "aiohttp", "openai",
}

SECRET = "ACME-INTERNAL-7741"


@pytest.fixture
def audit(write_csv):
    rows = ["reading_date,site_code,note,value,run_date"]
    for i in range(60):
        rows.append(
            f"2026-01-{(i % 28) + 1:02d},{'NORTH' if i % 2 else 'SOUTH'},"
            f"{SECRET}-{i},{i * 1.5},2026-08-27"
        )
    return run(write_csv("d.csv", "\n".join(rows) + "\n"))


class FakeProvider:
    """A test double at the network boundary. No socket is opened anywhere in
    this suite."""

    def __init__(self, data=None):
        self.data = data or {
            "declarations": {
                "time_axis": "reading_date",
                "grain": ["reading_date", "site_code"],
                "forecast_column": None,
            },
            "reasoning": "Daily readings for two sites.",
            "questions": [{
                "column": "value",
                "ask": "Is this a percentage?",
                "proposed_answer": "Probably not.",
                "why_it_matters": "Bound checks depend on it.",
            }],
            "unresolved": [],
        }
        self.model = "fake"
        self.seen = None

    def ask(self, system, prompt, schema):
        self.seen = {"system": system, "prompt": prompt, "schema": schema}
        return provider_mod.Response(data=self.data, model="fake",
                                     input_tokens=10, output_tokens=5)


class TestTheSchemaTheApiWillAccept:
    """Structured outputs rejects some JSON Schema keywords outright.

    `maxItems` on an array returns a 400 that fails the ENTIRE request, not the
    one field -- so a keyword added for tidiness silently breaks the only
    feature that talks to the network. The unit tests could not catch it,
    because the fake provider never validates the schema; only a live call did.
    These are the cheap guard that stands in for one.
    """

    UNSUPPORTED = ("maxItems", "minItems", "maxProperties", "minProperties",
                   "maxLength", "minLength", "pattern", "format")

    def _walk(self, node, path="schema"):
        found = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key in self.UNSUPPORTED:
                    found.append(f"{path}.{key}")
                found += self._walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                found += self._walk(item, f"{path}[{i}]")
        return found

    def test_the_schema_uses_no_rejected_keyword(self):
        assert self._walk(interview_mod.SCHEMA) == []

    def test_the_question_cap_is_enforced_in_code(self):
        """The cap the schema is not allowed to carry has to live somewhere."""
        many = {
            "declarations": {},
            "reasoning": "",
            "questions": [
                {"column": None, "ask": f"q{i}", "proposed_answer": None,
                 "why_it_matters": ""}
                for i in range(12)
            ],
            "unresolved": [],
        }
        out = interview_mod.run({}, FakeProvider(many))
        assert len(out.questions) == interview_mod.MAX_QUESTIONS


class TestTheBoundary:
    def test_no_core_module_imports_a_network_library(self):
        """The core promise, asserted rather than claimed.

        Everything but dataassay.llm must be reachable with no network library
        in sight — that is what makes "nothing leaves your machine" checkable.
        """
        root = Path(interview_mod.__file__).parent.parent
        offenders = []
        for py in sorted(root.rglob("*.py")):
            if py.parent.name == "llm":
                continue
            tree = ast.parse(py.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    if name in NETWORK_MODULES:
                        offenders.append(f"{py.relative_to(root)}: {name}")
        assert offenders == []

    def test_the_socket_block_is_actually_in_force(self):
        """Guard the guard.

        The autouse fixture in conftest is what makes every other test in this
        suite a no-network test. If it silently stopped working, nothing would
        fail -- the suite would just quietly go back to being allowed online,
        and the strongest claim the package makes would be unasserted. So it
        gets asserted directly.
        """
        import socket

        from conftest import NetworkAccessDenied

        with pytest.raises(NetworkAccessDenied):
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(NetworkAccessDenied):
            socket.create_connection(("example.invalid", 80))

    def test_a_full_audit_runs_with_the_network_torn_out(self, write_csv):
        """The product claim, end to end.

        Not "no core module imports a network library" (that is the static
        test) but "a real audit completes with no socket layer underneath it".
        This is the one a buyer who will not upload their data is actually
        asking about.
        """
        from dataassay import audit as audit_mod

        csv = write_csv("local.csv", "date,value\n" + "".join(
            f"2026-01-{d:02d},{d * 1.5}\n" for d in range(1, 29)))
        result = audit_mod.run(csv)
        assert result.profile.provenance.row_count == 28

    def test_the_adapter_is_optional(self):
        # Core must import and run with the extra absent.
        import dataassay.audit  # noqa: F401
        import dataassay.report  # noqa: F401

        root = Path(interview_mod.__file__).parent.parent
        core = (root / "audit.py").read_text() + (root / "cli.py").read_text()
        assert "from dataassay.llm import" not in core.split("_cmd_interview")[0]


class TestPayload:
    def test_no_row_of_data_reaches_it(self, audit):
        body = payload_mod.build(audit)
        blob = json.dumps(body, default=str)
        assert SECRET not in blob

    def test_low_cardinality_categories_are_included_and_declared(self, audit):
        body = payload_mod.build(audit)
        site = next(c for c in body["columns"] if c["name"] == "site_code")
        assert site["top_values_are_raw_values"] is True
        assert {v for v, _ in site["top_values"]} == {"NORTH", "SOUTH"}

    def test_redaction_strips_them(self, audit):
        body = payload_mod.build(audit, redact_values=True)
        site = next(c for c in body["columns"] if c["name"] == "site_code")
        assert "top_values" not in site
        assert body["values_included"] is False

    def test_the_local_path_is_not_sent(self, audit):
        body = payload_mod.build(audit)
        assert "path" not in body["file"]
        assert audit.profile.provenance.path not in json.dumps(body, default=str)

    def test_the_summary_names_what_is_included(self, audit):
        text = payload_mod.summarize(payload_mod.build(audit))
        assert "NOT included: any row of your data" in text
        assert "site_code" in text

    def test_the_summary_says_so_when_redacted(self, audit):
        text = payload_mod.summarize(payload_mod.build(audit, redact_values=True))
        assert "no cell contents at all" in text


class TestInterview:
    def test_proposals_land_in_proposed_never_declared(self, audit, tmp_path):
        # The manifest's one guarantee is that `declared` means a person said
        # so. A model writing into it would destroy exactly that.
        out = interview_mod.run(payload_mod.build(audit), FakeProvider())
        names = [c.name for c in audit.profile.columns]
        kept, dropped = interview_mod.validate(out.declarations, names)
        assert kept["time_axis"] == "reading_date"
        assert dropped == []

        from dataassay import manifest as manifest_mod
        m = manifest_mod.from_audit(audit.profile, audit.structure)
        m.proposed = kept
        target = tmp_path / "m.json"
        m.write(target)
        reloaded = manifest_mod.load(target)
        assert reloaded.proposed["time_axis"] == "reading_date"
        assert reloaded.declared == {}
        assert reloaded.declared_value("time_axis") is None

    def test_a_hallucinated_column_is_discarded(self, audit):
        fake = FakeProvider({
            "declarations": {"time_axis": "no_such_column", "grain": None,
                             "forecast_column": None},
            "reasoning": "", "questions": [], "unresolved": [],
        })
        out = interview_mod.run(payload_mod.build(audit), fake)
        kept, dropped = interview_mod.validate(
            out.declarations, [c.name for c in audit.profile.columns]
        )
        assert kept == {}
        assert "no such column" in dropped[0]

    def test_the_prompt_carries_the_payload_and_the_context(self, audit):
        fake = FakeProvider()
        interview_mod.run(payload_mod.build(audit), fake, context="Sensor feed.")
        assert "Sensor feed." in fake.seen["prompt"]
        assert "reading_date" in fake.seen["prompt"]

    def test_the_system_prompt_forbids_choosing_checks(self):
        # The engine gates checks deterministically. Handing that to a model
        # would throw away the discipline the tool is built on.
        assert "You do not choose which checks run" in interview_mod.SYSTEM
        assert "not given rows" in interview_mod.SYSTEM

    def test_the_question_budget_is_capped_somewhere(self):
        """This used to assert the cap lived in the schema, as `maxItems: 5`.

        It passed for as long as nothing made a real request. Structured
        outputs rejects `maxItems` with a 400 that fails the whole call, so the
        test was not merely testing the wrong thing -- it was pinning the
        defect in place, and would have failed the fix. The budget is real; the
        schema is just not where it can be expressed.
        """
        assert "maxItems" not in interview_mod.SCHEMA["properties"]["questions"]
        assert interview_mod.MAX_QUESTIONS == 5
        assert "at most five" in interview_mod.SYSTEM


class TestCredentials:
    def test_no_key_is_a_clear_message_not_a_traceback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        assert "never reads a key from a file" in provider_mod.NO_CREDENTIALS

    def test_an_unknown_provider_is_refused(self):
        with pytest.raises(provider_mod.ProviderError, match="Unknown provider"):
            provider_mod.get("gpt")
