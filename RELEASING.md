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

1. Push this repo to GitHub. Until then `.github/workflows/dataassay.yml` is
   inert — there is no remote today.
2. Create the project on PyPI by uploading the first release manually (PyPI
   cannot configure a publisher for a name that does not exist yet):
   ```
   .venv/bin/pip install twine
   .venv/bin/twine upload dist/*
   ```
3. Then configure the trusted publisher at
   `https://pypi.org/manage/project/dataassay/settings/publishing/`:
   - Owner / repository: this repo
   - Workflow: `dataassay.yml`
   - Environment: `pypi`
4. Every release after that is just a tag.

## Cutting a release

1. Bump `__version__` in `src/dataassay/__init__.py`. It is the single source of
   truth — `pyproject.toml` reads it via `[tool.hatch.version]`.
2. `./check.sh`
3. Commit, then tag with the package prefix (this is a monorepo, so a bare `v*`
   tag would be ambiguous):
   ```
   git tag dataassay-v0.0.2 && git push --tags
   ```

## Rules that are not negotiable

- **A version is forever.** PyPI never lets a version number be reused, and
  "deleting" a release does not free it. Burn `0.0.x` freely; do not touch
  `1.0.0` until the check catalog is real.
- **The package never imports from the parent repo.** Not `report_utils`, not
  `data_registry`, nothing. The dependency runs the other way: this repo becomes
  a *consumer* of dataassay. The clean-environment step in CI is what enforces
  it — if a monorepo import sneaks in, the wheel breaks there and not in
  production.
- **One runtime dependency.** Adding a second is a product decision, not a
  convenience. It gets argued before it gets typed.
