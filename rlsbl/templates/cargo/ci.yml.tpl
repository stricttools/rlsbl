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
{{#if minRequiredRust}}      # rust-version: {{minRequiredRust}}
{{/if}}      - uses: {{action "dtolnay/rust-toolchain"}}
      - run: cargo fmt --check
      - run: cargo clippy -- -D warnings
      - run: cargo test
