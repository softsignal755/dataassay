"""The manifest: an answer, recorded once, that outlives the conversation."""

import json

import pytest

from dataassay import manifest as manifest_mod
from dataassay.audit import run
from dataassay.checks.base import DEFECT

HEAD = "d,tag,payload\n"
BODY = "".join(f"2026-01-{(i % 28) + 1:02d},{'ab'[i % 2]},{i % 5}\n" for i in range(40))


@pytest.fixture
def data(write_csv):
    return write_csv("d.csv", HEAD + BODY)


def _write_manifest(data_file, **declared):
    a = run(data_file, use_manifest=False)
    m = manifest_mod.from_audit(a.profile, a.structure)
    m.declared = declared
    target = manifest_mod.path_for(data_file)
    m.write(target)
    return target


class TestAuthoring:
    def test_init_records_the_schema_and_the_questions(self, data):
        a = run(data, use_manifest=False)
        m = manifest_mod.from_audit(a.profile, a.structure)
        assert m.schema_columns == ["d", "tag", "payload"]
        assert "time_axis" in m.detected

    def test_declared_starts_empty(self, data):
        # Pre-filling would promote every guess to an answer the moment someone
        # saved the file, which is the failure the split exists to prevent.
        a = run(data, use_manifest=False)
        assert manifest_mod.from_audit(a.profile, a.structure).declared == {}

    def test_round_trips_through_json(self, data, tmp_path):
        target = _write_manifest(data, time_axis="d")
        loaded = manifest_mod.load(target)
        assert loaded.declared["time_axis"] == "d"
        assert loaded.schema_columns == ["d", "tag", "payload"]

    def test_wrong_version_is_refused(self, tmp_path):
        p = tmp_path / "m.json"
        p.write_text(json.dumps({"manifest_version": 99}))
        with pytest.raises(ValueError, match="manifest_version"):
            manifest_mod.load(p)


class TestPrecedence:
    def test_declared_beats_detected(self):
        m = manifest_mod.Manifest(detected={"time_axis": "a"},
                                  declared={"time_axis": "b"})
        assert m.get("time_axis") == ("b", "declared")

    def test_detected_is_used_when_nothing_is_declared(self):
        m = manifest_mod.Manifest(detected={"time_axis": "a"})
        assert m.get("time_axis") == ("a", "detected")

    def test_empty_declared_value_does_not_override(self):
        m = manifest_mod.Manifest(detected={"grain": ["a"]}, declared={"grain": []})
        assert m.get("grain") == (["a"], "detected")


class TestItChangesTheAudit:
    def test_declared_grain_unblocks_the_duplicate_check(self, write_csv):
        # Without a manifest, no combination of columns is unique, so the check
        # can only ask. With one, it can answer.
        body = "d,tag,v\n" + "".join(f"2026-01-01,x,{i % 4}\n" for i in range(40))
        f = write_csv("w.csv", body)
        before = run(f, use_manifest=False)
        assert any(c == "duplicate_grain" for c, _ in before.coverage.blocked)

        _write_manifest(f, grain=["d", "tag", "v"])
        after = run(f)
        assert "duplicate_grain" in after.coverage.ran
        assert after.structure.grain_declared is True

    def test_declared_grain_that_is_not_unique_reports_duplicates(self, data):
        _write_manifest(data, grain=["d"])
        a = run(data)
        assert [f for f in a.findings if f.check_id == "duplicate_grain"]

    def test_declared_time_axis_is_marked_as_declared(self, data):
        _write_manifest(data, time_axis="d")
        s = run(data).structure
        assert s.time_axis == "d"
        assert s.time_axis_declared is True
        assert "declared" in s.time_axis_basis

    def test_a_declared_column_that_does_not_exist_is_ignored_loudly(self, data):
        _write_manifest(data, time_axis="nope")
        s = run(data).structure
        assert s.time_axis_declared is False
        assert any("not a column" in x for x in s.assumptions)

    def test_no_manifest_flag_ignores_it(self, data):
        _write_manifest(data, time_axis="d")
        assert run(data, use_manifest=False).manifest_path is None


class TestSchemaDrift:
    def test_a_vanished_column_is_a_defect(self, write_csv, data):
        _write_manifest(data)
        # Same manifest, a file that lost a column.
        thinner = data.with_name("d.csv")
        thinner.write_text(
            "d,tag\n" + "".join(f"2026-01-{(i % 28) + 1:02d},{'ab'[i % 2]}\n"
                                for i in range(40))
        )
        f = [x for x in run(thinner).findings if x.check_id == "schema_drift"]
        assert f and f[0].disposition == DEFECT
        assert f[0].evidence["removed"] == ["payload"]

    def test_it_is_withheld_without_a_manifest(self, data):
        withheld = dict(run(data, use_manifest=False).coverage.withheld)
        assert "schema_drift" in withheld
        assert "no manifest" in withheld["schema_drift"]

    def test_an_unchanged_file_drifts_nothing(self, data):
        _write_manifest(data)
        assert not [f for f in run(data).findings if f.check_id == "schema_drift"]
