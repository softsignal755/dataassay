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
store, rotate, or leak. There is nothing to upload by hand — not even the first
release.

1. Configure a **pending publisher** at
   `https://pypi.org/manage/account/publishing/`. "Pending" is the form a
   trusted publisher takes for a project that does not exist yet: it creates the
   project the first time it publishes, and converts itself into a normal
   publisher at that moment. Fill in:
   - PyPI project name: `dataassay`
   - Owner / repository: this repo
   - Workflow: `ci.yml`
   - Environment: `pypi`
2. Create the `pypi` environment in GitHub: **Settings → Environments → New
   environment → `pypi`**. It holds no secrets — that is the point of OIDC — but
   the publish job declares `environment: pypi` and GitHub fails the job if the
   name does not exist.
3. Tag. The first `v*` tag creates the project on PyPI and publishes it.

An earlier version of this file said the first release had to go up manually
with twine because a publisher could not be configured for a name that does not
exist. That has not been true since PyPI added pending publishers, and following
it would have meant an unnecessary API token on the one path built to avoid one.

A **PyPI organization is not required to publish.** It governs shared ownership
and namespace management, nothing more. Organization requests are reviewed by
admins periodically with no committed timeline — waits of weeks are ordinary and
approvals have been paused outright before now — so do not sequence a release
behind one. Publish from the personal account; move the project into the
organization if and when it is approved.

## Cutting a release

1. Bump `__version__` in `src/dataassay/__init__.py`. It is the single source of
   truth — `pyproject.toml` reads it via `[tool.hatch.version]`.
2. `./check.sh`
3. Commit, then tag:
   ```
   git tag v0.7.0 && git push --tags
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
