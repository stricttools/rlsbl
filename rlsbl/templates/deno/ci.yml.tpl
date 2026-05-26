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
      - uses: {{action "denoland/setup-deno"}}
        with:
          deno-version: v2.x
      - run: deno check **/*.ts
      - run: deno test
