name: CI

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

jobs:
  test:
    strategy:
      matrix:
        os: [macos-latest, ubuntu-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "swift-actions/setup-swift"}}
      - run: swift build
      - run: swift test
