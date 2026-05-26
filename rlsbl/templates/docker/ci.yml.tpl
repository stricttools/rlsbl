name: CI

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "docker/setup-buildx-action"}}
      - uses: {{action "docker/build-push-action"}}
        with:
          context: .
          push: false
          cache-from: type=gha
          cache-to: type=gha,mode=max
