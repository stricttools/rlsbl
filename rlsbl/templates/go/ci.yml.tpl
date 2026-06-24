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
    steps:
      - uses: {{action "actions/checkout"}}
{{#if go.minRequiredGo}}      # go.mod: go {{go.minRequiredGo}}
{{/if}}      - uses: {{action "actions/setup-go"}}
        with:
          go-version-file: go.mod
      - run: go vet ./...
      - run: go test ./... -race -short -timeout=10m
