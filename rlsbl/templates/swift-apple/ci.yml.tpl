name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: macos-latest
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "swift-actions/setup-swift"}}
      - run: swift build
      - run: swift test
