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
      # rust-version: {{cargo.minRequiredRust}}
      - uses: {{action "dtolnay/rust-toolchain"}}
      - run: cargo fmt --check
      - run: cargo clippy -- -D warnings
      - run: cargo test
