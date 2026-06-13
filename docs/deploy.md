---
description: "SSH-based deployment system with configurable health checks, automatic rollback on failure, and branch restrictions for safe remote deploys."
---

# Deploy

`rlsbl deploy` runs SSH-based deployments to configured remote targets. It is a standalone command, not wired to the release pipeline or publish pipelines -- you invoke it manually (or from a post-release hook) when you want to push code to a server.

## Configuration

Deploy targets are defined in `.rlsbl/config.json` under the `deploy` key, which is a list of target objects. Each target specifies a remote host, SSH credentials, shell commands to execute, branch restrictions, and optional health check and rollback configurations.

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
| `directory` | string | (none) | Working directory for local steps (`cwd`) and remote steps (`cd`) |
| `local_steps` | array of strings | (none) | Shell commands executed locally before SSH steps (e.g., cross-compilation, rsync). Supports `$VAR` expansion. |
| `env` | object | (none) | Environment variables exported before running remote steps |
| `health` | object | (none) | Health check configuration (see below) |
| `rollback_steps` | array of strings | (none) | Commands to execute if health check fails after deployment |

## Local steps

The `local_steps` field runs shell commands on the local machine before any SSH steps execute. This is useful for build steps that must happen locally (cross-compilation, asset bundling) and file transfers (rsync, scp) that move artifacts to the remote host before the remote steps configure and restart services.

Local steps execute sequentially via `subprocess.run(step, shell=True, check=True)`. If any local step fails (non-zero exit), the deploy aborts immediately -- no SSH steps or health checks run. Environment variable references (`$VAR`) in local steps are expanded before execution, using the same expansion as `host` and `ssh_key`.

When `directory` is configured, it is used as the `cwd` for local step execution (same directory is used as the remote `cd` target for SSH steps).

### Execution order

1. Branch restriction check
2. Local steps (if configured)
3. SSH steps
4. Health check (if configured)
5. Rollback (if health check fails and `rollback_steps` configured)

## Environment variable expansion

The `host` and `ssh_key` fields support `$VAR` syntax for referencing environment variables. Expansion happens from the process environment (`os.environ`) at deploy time — both during config validation and during execution.

The pattern matches `$` followed by a valid identifier (`[A-Za-z_][A-Za-z0-9_]*`). If the referenced variable is not set in the environment, the deploy fails with a hard error: `Environment variable $VAR is not set`.

Examples:

- `"host": "$DEPLOY_HOST"` resolves from `os.environ["DEPLOY_HOST"]`
- `"ssh_key": "$HOME/.ssh/deploy_key"` expands `$HOME` from the environment, leaving `/.ssh/deploy_key` as a literal suffix
- `"host": "10.0.0.$SUFFIX"` expands only `$SUFFIX`, keeping the prefix literal

Multiple variables in a single value are expanded independently. The expansion is not recursive — the result of expanding one variable is not scanned for further `$` references.

The `env` field (environment variables exported on the remote host) does NOT undergo local expansion. Those values are passed as-is to the remote shell.

## Health checks

Health checks verify that the deployment succeeded by probing the remote service after all deploy steps complete. Three probe types are supported, each with a configurable timeout (default 30 seconds) and type-specific connection parameters:

| Type | Required fields | Optional fields | Behavior |
| --- | --- | --- | --- |
| `http` | `url` | `timeout` (default 30s), `interval` (default 3s) | Polls URL until HTTP 200 or timeout |
| `tcp` | `port` | `host` (defaults to deploy host), `timeout` (default 30s) | Attempts TCP connection until success or timeout |
| `script` | `command` | `timeout` (default 30s) | Executes command via SSH; exit 0 = healthy |

Health checks run immediately after all steps complete. If no health check is configured, a warning is printed to stderr and the deploy reports success without verification.

## Automatic rollback

When a health check fails and `rollback_steps` is configured, rlsbl executes the rollback sequence using the same SSH connection parameters (host, user, directory, env) as the original deploy steps, then re-runs the health check to verify service recovery. The outcome is reported as one of four states depending on whether rollback was attempted and whether the service recovered:

1. Each rollback step executes sequentially via SSH (same host/user/directory/env as deploy steps)
2. After rollback completes, the health check re-runs
3. If health passes after rollback: returns failure with `rolled_back=True` and message "Rollback successful, service restored"
4. If health fails after rollback: returns failure with `rolled_back=True` and message "Rollback failed, manual intervention required"

If no `rollback_steps` are configured and health fails, the deploy simply reports the health check failure.

## Branch restrictions

The `only_on` field restricts which git branches can deploy to each target, preventing accidental deployments from feature branches or other non-release branches. The field is required and must contain at least one branch name. When the current branch is not in the list:

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
  "deploy": [
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

## Example config with local steps

This configuration cross-compiles a Go binary on the local machine, transfers the build artifact to the remote host via rsync, and then restarts the service remotely over SSH. The `local_steps` run on your machine before any remote connection is made, so the remote host never needs a build toolchain installed — it only receives the finished binary:

```json
{
  "deploy": [
    {
      "name": "production",
      "host": "$DEPLOY_HOST",
      "user": "deploy",
      "ssh_key": "$HOME/.ssh/deploy_key",
      "directory": "/opt/myapp",
      "only_on": ["main"],
      "local_steps": [
        "GOOS=linux GOARCH=amd64 go build -o dist/myapp ./cmd/myapp",
        "rsync -avz --progress dist/myapp deploy@$DEPLOY_HOST:/opt/myapp/myapp"
      ],
      "steps": [
        "chmod +x /opt/myapp/myapp",
        "systemctl restart myapp"
      ],
      "health": {
        "type": "http",
        "url": "http://localhost:8080/health",
        "timeout": 30
      }
    }
  ]
}
```

## Deploy module

The deploy module implements target resolution, branch restriction enforcement, dry-run simulation, and the orchestration logic that connects named deploy configurations to their execution pipelines. It reads from `.rlsbl/config.json` and validates all preconditions before executing any deployment step.

:-: ref path="rlsbl.deploy" lang="python"
