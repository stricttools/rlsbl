name: CI

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
      - name: Validate spec
        run: echo "Add validation commands here"
