"""Rendering and exit codes. The CLI decides nothing; it only shows."""

import json

import pytest

CLEAN = "id,amount,when\n" + "".join(
    f"{i},{i * 3},2026-01-{i:02d}\n" for i in range(1, 28)
)
AMBIGUOUS = "id,amount,when\n1,1.234,03/04/2026\n2,5.678,05/06/2026\n"


@pytest.fixture
def main():
    from dataassay.cli import main as _main

    return _main


def test_json_output_is_the_machine_contract(main, write_csv, capsys):
    assert main(["profile", str(write_csv("a.csv", CLEAN)), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["provenance"]["row_count"] == 27


def test_text_output_shows_columns_and_questions(main, write_csv, capsys):
    assert main(["profile", str(write_csv("a.csv", AMBIGUOUS))]) == 0
    out = capsys.readouterr().out
    assert "NEEDS YOUR ANSWER" in out
    assert "1000x" in out
    assert "amount" in out


def test_clean_file_reports_no_questions(main, write_csv, capsys):
    assert main(["profile", str(write_csv("a.csv", CLEAN))]) == 0
    assert "0 open questions" in capsys.readouterr().out


def test_questions_do_not_fail_the_run_by_default(main, write_csv):
    # An unanswered question is a coverage gap, not an error. The profile is
    # still complete and useful without an answer.
    assert main(["profile", str(write_csv("a.csv", AMBIGUOUS))]) == 0


def test_fail_on_question_is_opt_in_for_pipelines(main, write_csv, capsys):
    assert main(["profile", str(write_csv("a.csv", AMBIGUOUS)), "--fail-on-question"]) == 1
    capsys.readouterr()
    assert main(["profile", str(write_csv("b.csv", CLEAN)), "--fail-on-question"]) == 0


def test_missing_file_is_exit_2(main, tmp_path, capsys):
    assert main(["profile", str(tmp_path / "nope.csv")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_unsupported_format_is_exit_2(main, tmp_path, capsys):
    p = tmp_path / "book.xlsx"
    p.write_bytes(b"nope")
    assert main(["profile", str(p)]) == 2
    assert "no reader" in capsys.readouterr().err
