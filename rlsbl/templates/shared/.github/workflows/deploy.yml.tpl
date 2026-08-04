name: Deploy

on:
  push:
    tags:
      - 'v*'
  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "actions/setup-python"}}
        with:
          python-version: '3.11'
      - run: pip install rlsbl
      # --approve-consequential: `deploy` declares itself consequential and CI
      # has no interactive stdin, so without it the framework confirm protocol
      # aborts the step.
      - run: rlsbl deploy --approve-consequential
        env:
          DEPLOY_SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
