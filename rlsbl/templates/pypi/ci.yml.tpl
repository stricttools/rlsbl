name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

{{#if uvNoSources}}
env:
  UV_NO_SOURCES: "1"

{{/if}}
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
      - run: uv sync
      - run: uv run python -c "import {{importName}}"
