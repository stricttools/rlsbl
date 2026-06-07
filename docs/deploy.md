---
description: "SSH-based deployment system with health checks, automatic rollback, and branch restrictions for remote targets."
---

# Deploy

`rlsbl deploy` runs SSH-based deployments to configured remote targets. It is a standalone command, not wired to the release pipeline or publish pipelines -- you invoke it manually (or from a post-release hook) when you want to push code to a server.

## Configuration

Deploy targets are defined in `.rlsbl/config.json` under the `deploy_targets` key, which is a list of target objects.

### Required fields

| Field | Type | Description |
| --- | --- | --- |
| `name` | string | Target identifier (must be unique across all targets) |
| `host` | string | Remote hostname or IP. Supports `$VAR` expansion from environment. |
| `steps` | array of strings | Shell commands executed sequentially on the remote host |
| `only_on` | array of strings | Branch names where deployment is allowed (non-empty) |

### Optional fields

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `user` | string | `"root"` | SSH user for the connection |
| `ssh_key` | string | (none) | Path to private key file. Supports `$VAR` expansion. |
| `directory` | string | (none) | Working directory on the remote host (commands `cd` here first) |
| `env` | object | (none) | Environment variables exported before running steps |
| `health` | object | (none) | Health check configuration (see below) |
| `rollback_steps` | array of strings | (none) | Commands to execute if health check fails after deployment |

## Health checks

Health checks verify that the deployment succeeded by probing the remote service. Three types are supported:

| Type | Required fields | Optional fields | Behavior |
| --- | --- | --- | --- |
| `http` | `url` | `timeout` (default 30s), `interval` (default 3s) | Polls URL until HTTP 200 or timeout |
| `tcp` | `port` | `host` (defaults to deploy host), `timeout` (default 30s) | Attempts TCP connection until success or timeout |
| `script` | `command` | `timeout` (default 30s) | Executes command via SSH; exit 0 = healthy |

Health checks run immediately after all steps complete. If no health check is configured, a warning is printed to stderr and the deploy reports success without verification.

## Automatic rollback

When a health check fails and `rollback_steps` is configured:

1. Each rollback step executes sequentially via SSH (same host/user/directory/env as deploy steps)
2. After rollback completes, the health check re-runs
3. If health passes after rollback: returns failure with `rolled_back=True` and message "Rollback successful, service restored"
4. If health fails after rollback: returns failure with `rolled_back=True` and message "Rollback failed, manual intervention required"

If no `rollback_steps` are configured and health fails, the deploy simply reports the health check failure.

## Branch restrictions

The `only_on` field restricts which git branches can deploy to each target. When the current branch is not in the list:

- Without `--force`: the command exits with an error showing allowed branches
- With `--force`: the restriction is bypassed and deployment proceeds

## Flags

| Flag | Description |
| --- | --- |
| `--dry-run` | Print what would be deployed (target info, steps, health config) without executing |
| `--force` | Override branch restrictions |
| (positional) | Target name. Auto-selects if only one target is configured. Required when multiple targets exist. |

## SSH execution details

- `StrictHostKeyChecking=accept-new` -- new hosts are accepted automatically, changed keys still fail
- `BatchMode=yes` -- no interactive prompts (fails immediately if auth requires interaction)
- Commands are chained with `&&` on the remote side (env exports, cd, then the step command)
- Default timeout: 120 seconds per step
- Environment variables from `env` config are shell-escaped and exported before the command

## Example config

```json
{
  "deploy_targets": [
    {
      "name": "production",
      "host": "$DEPLOY_HOST",
      "user": "deploy",
      "ssh_key": "$HOME/.ssh/deploy_key",
      "directory": "/opt/myapp",
      "only_on": ["main"],
      "env": {
        "NODE_ENV": "production",
        "PORT": "3000"
      },
      "steps": [
        "git pull origin main",
        "npm ci --production",
        "systemctl restart myapp"
      ],
      "health": {
        "type": "http",
        "url": "http://localhost:3000/health",
        "timeout": 60,
        "interval": 5
      },
      "rollback_steps": [
        "git checkout HEAD~1",
        "npm ci --production",
        "systemctl restart myapp"
      ]
    }
  ]
}
```

## Deploy module

:-: ref path="rlsbl.deploy" lang="python"
