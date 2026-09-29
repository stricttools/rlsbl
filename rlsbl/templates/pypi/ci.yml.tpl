name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

# Per-SHA group: re-runs of the same commit dedupe, but a new commit never
# cancels an earlier commit's in-flight run (release CI conclusions stay intact).
concurrency:
  group: ${{ github.workflow_ref }}-${{ github.sha }}
  cancel-in-progress: true

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
{{#if pypi.minRequiredPython}}        # requires-python: >= {{pypi.minRequiredPython}}
{{/if}}        python-version: ["3.12", "3.13", "3.14"]
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "astral-sh/setup-uv"}}
      - run: uv python install ${{ matrix.python-version }}
      - run: uv sync --locked
      - run: uv run python -c "import {{importName}}"{{#if pypi.hasPytest}}
      - run: uv run pytest --rootdir .{{/if}}{{#if pypi.nestedMembers}}
      # Workspace members nested in this one are published on their own; the
      # upload must not carry their files. Listing an upload's contents takes
      # building it, which needs the network, so it is checked here, on the
      # release commit, rather than before the release starts.
      - name: Check the upload leaves nested members out
        run: |
          uv build --out-dir "$RUNNER_TEMP/rlsbl-upload"
          python3 - "$RUNNER_TEMP/rlsbl-upload" {{pypi.nestedMembers}} <<'PY'
          import pathlib, sys, tarfile, zipfile
          dist = pathlib.Path(sys.argv[1])
          nested = [n.strip("/") for n in sys.argv[2:]]
          def inside(rel):
              return any(rel == n or rel.startswith(n + "/") for n in nested)
          def files(base):
              return [p.as_posix() for p in pathlib.Path(base).rglob("*")
                      if p.is_file() and not any(part.startswith(".") for part in p.parts)]
          nested_files = [f for n in nested for f in files(n)]
          own_files = [f for f in files(".") if not inside(f)]
          def provided(name, pool):
              return any(f == name or f.endswith("/" + name) for f in pool)
          bad = []
          for sdist in sorted(dist.glob("*.tar.gz")):
              with tarfile.open(sdist) as archive:
                  for name in archive.getnames():
                      rel = name.split("/", 1)[1] if "/" in name else ""
                      if rel and inside(rel):
                          bad.append(f"{sdist.name}: {rel}")
          for wheel in sorted(dist.glob("*.whl")):
              with zipfile.ZipFile(wheel) as archive:
                  for name in archive.namelist():
                      if ".dist-info/" in name or name.endswith("/"):
                          continue
                      if provided(name, nested_files) and not provided(name, own_files):
                          bad.append(f"{wheel.name}: {name}")
          if bad:
              print("::error::this package's upload ships files that the workspace "
                    "members nested inside it own: " + ", ".join(bad) + ". Leave "
                    "them out of the build: for hatchling, list the nested members' "
                    "directories under exclude in [tool.hatch.build.targets.sdist] "
                    "(and [tool.hatch.build.targets.wheel]).")
              sys.exit(1)
          print("the upload carries no nested member's files")
          PY{{/if}}
