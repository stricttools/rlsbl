name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        # requires-python: >= {{pypi.minRequiredPython}}
        python-version: ["3.12", "3.13", "3.14"]
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "astral-sh/setup-uv"}}
      - run: uv python install ${{ matrix.python-version }}
      - run: uv sync --no-sources
      - run: uv run python -c "import {{importName}}"
