# Releasing dataassay

## Local gate

```
./check.sh
```

Creates/uses `.venv`, lints, tests, builds, and installs the wheel into a
throwaway environment to confirm the dependency surface is still exactly
`dataassay + duckdb`. CI runs the same gate across four Python versions and
three operating systems.

## One-time PyPI setup

CI publishes via **Trusted Publishing** (OIDC), so there is no API token to
store or rotate.

1. Create the project on PyPI by uploading the first release manually. PyPI
   cannot configure a trusted publisher for a name that does not exist yet, so
   the first upload is always a manual one:
   ```
   .venv/bin/pip install twine
   .venv/bin/twine upload dist/*
   ```
2. Then configure the trusted publisher at
   `https://pypi.org/manage/project/dataassay/settings/publishing/`:
   - Owner / repository: this repo
   - Workflow: `ci.yml`
   - Environment: `pypi`
3. Every release after that is just a tag.

## Cutting a release

1. Bump `__version__` in `src/dataassay/__init__.py`. It is the single source of
   truth — `pyproject.toml` reads it via `[tool.hatch.version]`.
2. `./check.sh`
3. Commit, then tag:
   ```
   git tag v0.6.2 && git push --tags
   ```
   The tag is what triggers publication — `ci.yml` runs the full gate on every
   push, but only a `v*` tag reaches the `publish` job.

## Rules that are not negotiable

- **A version is forever.** PyPI never lets a version number be reused, and
  "deleting" a release does not free it. Burn `0.0.x` freely; do not touch
  `1.0.0` until the check catalog is real.
- **The package never imports from the project that produced it.** dataassay was
  extracted from a private data pipeline, and the dependency runs one way only:
  that pipeline is a *consumer* of this package, never the reverse. Nothing in
  `src/` may reach for `report_utils`, `data_registry`, or anything else outside
  this repo. There is no longer a parent on disk to import by accident, which
  means the clean-environment step in CI is now the *only* thing enforcing this —
  it installs the built wheel into an empty venv, so a stray import fails there
  rather than in someone's production run.
- **One runtime dependency.** Adding a second is a product decision, not a
  convenience. It gets argued before it gets typed.
