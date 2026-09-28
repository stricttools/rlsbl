name: Deploy

# Starts from a published GitHub Release, like every rlsbl publish workflow,
# never from a tag push: GitHub fires no tag-push events for a push carrying
# more than three tags, and a tag filter such as 'v*' never matches
# 'name@v1.2.3' or 'path/v1.2.3' tags. Dispatch at a tag re-runs a deploy.
on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      tag:
        description: "Release tag to deploy (e.g. v1.2.3). Overrides the ref for retry dispatch."
        required: false
        type: string

# One deploy run per tag: a dispatch at the same tag queues behind the
# in-flight run instead of racing it. A deploy is never cancelled mid-flight.
concurrency:
  group: deploy-${{ inputs.tag || github.ref_name }}
  cancel-in-progress: false

permissions:
  contents: read

jobs:
{{publishGate}}
  deploy:
    needs: gate
    runs-on: ubuntu-latest
    steps:
      # The tag's checkout is a detached HEAD, so `rlsbl deploy` holds the
      # target's only_on against origin's branches: the tagged commit must be
      # reachable from one of them. fetch-depth: 0 brings those branches and
      # the history that answers it.
      - uses: {{action "actions/checkout"}}
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
          fetch-depth: 0
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
