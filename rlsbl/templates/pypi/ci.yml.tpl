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
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - run: uv run pytest --rootdir .{{/if}}
