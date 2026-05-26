name: CI

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "erlef/setup-beam"}}
        with:
          otp-version: "26"
          elixir-version: "1.16"
      - run: mix deps.get
      - run: mix format --check-formatted
      - run: mix test
