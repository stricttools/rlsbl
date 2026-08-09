# sdist hygiene: allowlist include-block + no-bypass check

## Context

Fleet sdists ship the whole tree (hatchling default): todo/, .rlsbl/, .claude/, locks. An
empirical survey (2026-08-09, 8 packages + 4 back-versions) found real leaks reached PyPI
this way — private screenshots in one project's todo/ across four versions, a production
server inventory in a done-todo, cross-project names in todo prose. Wheels are clean
everywhere; this is sdist-only. Projects on uv_build or explicit include lists are already
clean.

## Decision (ratified 2026-08-09)

Allowlist, never denylist (a denylist silently leaks each new dot-dir):

- Scaffold emits `[tool.hatch.build.targets.sdist] include = ["src/", "tests/", "docs/",
  "README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml", "conftest.py"]` (three-way
  merged) into every hatchling pyproject — per MEMBER in monorepos (sdists build
  per-member; the root cannot cover them).
- New `sdist-hygiene` check (tags project + preflight): build the sdist, hard-error on any
  member matching todo/, .claude/, .rlsbl/, .selfdoc/, .strictcli/, *.local-only, or a
  non-fixture binary. No bypass flag.
- todo/ excluded hard; .rlsbl/ excluded (CHANGELOG.md + tag + GitHub Release already carry
  the provenance); tests/docs/scripts stay in.

## Effort

Medium: template + check + fleet propagation ride re-scaffolds.
