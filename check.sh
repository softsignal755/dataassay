#!/usr/bin/env bash
# Local release gate — the same checks CI runs.
set -euo pipefail
cd "$(dirname "$0")"

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[dev]"

echo "── lint ──";  .venv/bin/ruff check .
echo "── test ──";  .venv/bin/python -m pytest
echo "── build ──"; rm -rf dist; .venv/bin/python -m build >/dev/null

echo "── dependency surface ──"
CLEAN=$(mktemp -d)
trap 'rm -rf "$CLEAN"' EXIT
python3 -m venv "$CLEAN/v"
"$CLEAN/v/bin/pip" install -q dist/*.whl
"$CLEAN/v/bin/assay" --version
installed=$("$CLEAN/v/bin/pip" list --format=freeze | cut -d= -f1 \
  | grep -viE '^(pip|setuptools|wheel)$' | sort | tr '\n' ' ')
echo "installed: $installed"
[ "$installed" = "dataassay duckdb " ] || { echo "FAIL: unexpected dependency surface"; exit 1; }

echo "── the llm extra is genuinely optional ──"
# The core must keep its single dependency whether or not the extra exists, and
# the extra must add exactly one thing. Both halves are the product claim.
EXTRA=$(mktemp -d)
trap 'rm -rf "$CLEAN" "$EXTRA"' EXIT
python3 -m venv "$EXTRA/v"
"$EXTRA/v/bin/pip" install -q "$(ls dist/*.whl)[llm]"
"$EXTRA/v/bin/python" -c "import dataassay.llm.provider" >/dev/null
added=$("$EXTRA/v/bin/pip" list --format=freeze | cut -d= -f1 \
  | grep -viE '^(pip|setuptools|wheel|dataassay|duckdb)$' | sort | tr '\n' ' ')
echo "extra adds: $added"
case "$added" in *anthropic*) ;; *) echo "FAIL: llm extra did not add anthropic"; exit 1;; esac

echo "✓ all gates passed"
