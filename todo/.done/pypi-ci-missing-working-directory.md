# Scaffolded pypi CI workflow ignores the target's subdirectory path

## Problem

For a project whose pypi target lives in a subdirectory (config target
`{"name": "pypi", "path": "pypi/"}`), `rlsbl scaffold` generates a
`.github/workflows/ci-pypi.yml` whose steps run at the repo root:

```yaml
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
      - run: uv python install ${{ matrix.python-version }}
      - run: uv sync --locked
      - run: uv run python -c "import <pkg>"
```

`uv sync --locked` then fails on CI with:

```
error: No `pyproject.toml` found in current directory or any parent directory
```

because `pyproject.toml` / `uv.lock` live under the target path
(`pypi/`), not the repo root. This is a hard CI failure. In a repo whose
publish workflow gates on CI (the standard scaffold), it also blocks the
npm and pypi publish jobs, so a release tags fine but never publishes the
packages.

## Root cause

The pypi CI scaffold template emits the `uv` steps without honoring the
pypi target's `path`. It works only when the pypi target is at the repo
root. Go and npm multi-target repos likely have the same class of bug for
any non-root target path.

## Fix

When a pypi (or any) target has a non-root `path`, the scaffolded CI job
must run its build/test steps in that directory, e.g. a job-level default:

```yaml
    defaults:
      run:
        working-directory: pypi
```

or per-step `working-directory`. The scaffold already knows the target
path from config; thread it into the CI template. Audit the go and npm CI
templates for the same non-root-path assumption.

## Workaround applied downstream

A consumer added the `defaults.run.working-directory` block by hand and
released a hotfix to get green CI + publishing. Because scaffold does a
three-way merge, that customization should survive re-scaffold, but the
template itself should generate it correctly so new repos are not born
broken.

## Effort

Small: thread target `path` into the CI templates and default the
working directory. Add a scaffold test covering a non-root pypi target.
