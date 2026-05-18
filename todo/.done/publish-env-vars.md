# Built-in publish step lacks environment variables for deploys

## Problem

When rlsbl's built-in publish step runs `selfdoc deploy` for docs targets, it fails because Cloudflare credentials (`CF_PAGES_API_TOKEN`, `CF_ACCOUNT_ID`) aren't in the environment. These variables live in `~/Projects/.env` and need to be sourced before the deploy.

The post-release hook can work around this by sourcing the env file (`set -a; source ~/Projects/.env; set +a`), but the built-in publish step runs before the post-release hook and has no mechanism to load environment variables.

This causes every release to emit a warning:
```
Warning: docs target publish failed: Command '['selfdoc', 'deploy']' returned non-zero exit status 1.
```

## Proposed fix

Option A: Before running publish commands, source `~/Projects/.env` (or a configurable env file path) if it exists.

Option B: Add a `publish_env` or `env_file` config option in `.rlsbl/config.json` that specifies a file to source before publish steps.

Option C: Move docs publishing entirely to the post-release hook and remove the built-in docs publish step. Let the hook own the deploy.

## Affected projects

Any project using selfdoc with Cloudflare Pages deployment (safegit, and likely others).

## Effort

Small for option C. Medium for options A/B.
