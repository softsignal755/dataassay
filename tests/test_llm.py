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

    def test_the_schema_caps_the_question_budget(self):
        assert interview_mod.SCHEMA["properties"]["questions"]["maxItems"] == 5


class TestCredentials:
    def test_no_key_is_a_clear_message_not_a_traceback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        assert "never reads a key from a file" in provider_mod.NO_CREDENTIALS

    def test_an_unknown_provider_is_refused(self):
        with pytest.raises(provider_mod.ProviderError, match="Unknown provider"):
            provider_mod.get("gpt")
