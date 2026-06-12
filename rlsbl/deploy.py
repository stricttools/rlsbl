"""SSH deployment primitives providing config validation, remote command execution, health checks, and automatic rollback on failure."""

import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

from .errors import ConfigError


REQUIRED_DEPLOY_FIELDS = {"name", "host", "steps", "only_on"}


class DeployResult:
    """Result of a deploy operation."""

    def __init__(self, target_name, success, message, rolled_back=False):
        self.target_name = target_name
        self.success = success
        self.message = message
        self.rolled_back = rolled_back


def expand_env_vars(value):
    """Expand $VAR references in a string. Raises ValueError if var not set."""
    def replacer(match):
        var_name = match.group(1)
        val = os.environ.get(var_name)
        if val is None:
            raise ConfigError(f"Environment variable ${var_name} is not set")
        return val

    return re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", replacer, value)


def validate_deploy_config(targets):
    """Validate deploy targets from config. Returns list of errors (empty = valid)."""
    errors = []

    if not isinstance(targets, list):
        return ["deploy config must be a list of targets"]

    seen_names = set()

    for i, target in enumerate(targets):
        prefix = f"target[{i}]"

        if not isinstance(target, dict):
            errors.append(f"{prefix}: must be a dict")
            continue

        # Check required fields
        for field in REQUIRED_DEPLOY_FIELDS:
            if field not in target:
                errors.append(f"{prefix}: missing required field '{field}'")

        # Validate name uniqueness
        name = target.get("name")
        if name is not None:
            if not isinstance(name, str):
                errors.append(f"{prefix}: 'name' must be a string")
            elif name in seen_names:
                errors.append(f"{prefix}: duplicate target name '{name}'")
            else:
                seen_names.add(name)
                prefix = f"target '{name}'"

        # Validate only_on
        only_on = target.get("only_on")
        if only_on is not None:
            if not isinstance(only_on, list) or len(only_on) == 0:
                errors.append(f"{prefix}: 'only_on' must be a non-empty list of strings")
            elif not all(isinstance(b, str) for b in only_on):
                errors.append(f"{prefix}: 'only_on' must be a non-empty list of strings")

        # Validate steps
        steps = target.get("steps")
        if steps is not None:
            if not isinstance(steps, list) or len(steps) == 0:
                errors.append(f"{prefix}: 'steps' must be a non-empty list of strings")
            elif not all(isinstance(s, str) for s in steps):
                errors.append(f"{prefix}: 'steps' must be a non-empty list of strings")

        # Validate local_steps (optional)
        local_steps = target.get("local_steps")
        if local_steps is not None:
            if not isinstance(local_steps, list) or len(local_steps) == 0:
                errors.append(f"{prefix}: 'local_steps' must be a non-empty list of strings")
            elif not all(isinstance(s, str) for s in local_steps):
                errors.append(f"{prefix}: 'local_steps' must be a non-empty list of strings")

        # Expand and validate env var references in host and ssh_key
        host = target.get("host")
        if host is not None:
            if not isinstance(host, str):
                errors.append(f"{prefix}: 'host' must be a string")
            elif "$" in host:
                try:
                    expand_env_vars(host)
                except ConfigError as e:
                    errors.append(f"{prefix}: {e}")

        ssh_key = target.get("ssh_key")
        if ssh_key is not None:
            if not isinstance(ssh_key, str):
                errors.append(f"{prefix}: 'ssh_key' must be a string")
            elif "$" in ssh_key:
                try:
                    expand_env_vars(ssh_key)
                except ConfigError as e:
                    errors.append(f"{prefix}: {e}")

        # Validate health check config
        health = target.get("health")
        if health is not None:
            if not isinstance(health, dict):
                errors.append(f"{prefix}: 'health' must be a dict")
            else:
                errors.extend(_validate_health_config(health, prefix))

    return errors


def _validate_health_config(health, prefix):
    """Validate a health check config dict. Returns list of errors."""
    errors = []
    htype = health.get("type")

    if htype is None:
        errors.append(f"{prefix}: health check missing required field 'type'")
        return errors

    if htype not in ("http", "tcp", "script"):
        errors.append(f"{prefix}: health check type must be 'http', 'tcp', or 'script', got '{htype}'")
        return errors

    if htype == "http":
        if "url" not in health:
            errors.append(f"{prefix}: HTTP health check missing required field 'url'")
    elif htype == "tcp":
        if "port" not in health:
            errors.append(f"{prefix}: TCP health check missing required field 'port'")
        elif not isinstance(health["port"], int):
            errors.append(f"{prefix}: TCP health check 'port' must be an integer")
    elif htype == "script":
        if "command" not in health:
            errors.append(f"{prefix}: script health check missing required field 'command'")

    return errors


def ssh_run(host, user, command, ssh_key=None, directory=None, env=None, timeout=120):
    """Execute a command on a remote host via SSH. Returns (stdout, stderr, returncode)."""
    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
    ]

    if ssh_key:
        ssh_cmd.extend(["-i", ssh_key])

    ssh_cmd.append(f"{user}@{host}")

    # Build the remote command
    remote_parts = []

    if env:
        for key, value in env.items():
            # Shell-escape the value
            escaped = value.replace("'", "'\\''")
            remote_parts.append(f"export {key}='{escaped}'")

    if directory:
        remote_parts.append(f"cd {directory}")

    remote_parts.append(command)
    remote_command = " && ".join(remote_parts)
    ssh_cmd.append(remote_command)

    result = subprocess.run(
        ssh_cmd, capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout, result.stderr, result.returncode


def check_health(config, deploy_host, user="root"):
    """Run a health check based on config. Returns (success: bool, message: str)."""
    htype = config["type"]

    if htype == "http":
        return _check_health_http(config)
    elif htype == "tcp":
        return _check_health_tcp(config, deploy_host)
    elif htype == "script":
        return _check_health_script(config, deploy_host, user)
    else:
        return False, f"Unknown health check type: {htype}"


def _check_health_http(config):
    """Poll an HTTP URL until 200 or timeout."""
    url = config["url"]
    timeout = config.get("timeout", 30)
    interval = config.get("interval", 3)

    deadline = time.monotonic() + timeout
    last_error = None

    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            if resp.status == 200:
                return True, f"HTTP health check passed: {url}"
        except Exception as e:
            last_error = str(e)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    return False, f"HTTP health check failed after {timeout}s: {last_error or 'no 200 response'}"


def _check_health_tcp(config, deploy_host):
    """Try TCP connection until success or timeout."""
    host = config.get("host", deploy_host)
    port = config["port"]
    timeout = config.get("timeout", 30)

    deadline = time.monotonic() + timeout
    last_error = None

    while time.monotonic() < deadline:
        try:
            conn = socket.create_connection((host, port), timeout=5)
            conn.close()
            return True, f"TCP health check passed: {host}:{port}"
        except Exception as e:
            last_error = str(e)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(3, remaining))

    return False, f"TCP health check failed after {timeout}s: {last_error or 'connection refused'}"


def _check_health_script(config, deploy_host, user="root"):
    """Run a script health check via SSH on the deploy host."""
    command = config["command"]
    timeout = config.get("timeout", 30)

    stdout, stderr, returncode = ssh_run(
        host=deploy_host,
        user=user,
        command=command,
        timeout=timeout,
    )

    if returncode == 0:
        return True, f"Script health check passed: {command}"
    return False, f"Script health check failed (exit {returncode}): {stderr or stdout}"


def deploy_target(target_config, current_branch):
    """Deploy to a single target. Returns a DeployResult."""
    name = target_config["name"]
    only_on = target_config["only_on"]

    # 1. Branch restriction
    if current_branch not in only_on:
        return DeployResult(
            name, True,
            f"Skipped: branch '{current_branch}' not in only_on {only_on}",
        )

    # Expand env vars in host and ssh_key
    host = expand_env_vars(target_config["host"])
    user = target_config.get("user", "root")
    ssh_key = target_config.get("ssh_key")
    if ssh_key:
        ssh_key = expand_env_vars(ssh_key)
    directory = target_config.get("directory")
    env = target_config.get("env")
    local_steps = target_config.get("local_steps")
    steps = target_config["steps"]

    # 2. Run local steps (before SSH)
    if local_steps:
        for i, step in enumerate(local_steps):
            expanded_step = expand_env_vars(step)
            # The print below echoes the fully expanded command as an audit
            # trail before execution. expanded_step is operator-owned config
            # (deploy local_steps); shell semantics (env-assignment prefixes,
            # quoting, redirection) are an intentional contract, so shell=True
            # is deliberate. No timeout: local steps are unbounded build
            # commands. Output streams live to the terminal; on failure the
            # DeployResult message carries the expanded command.
            print(f"[{name}] Local step {i + 1}/{len(local_steps)}: {expanded_step}")
            try:
                subprocess.run(
                    expanded_step, shell=True, check=True,
                    cwd=directory,
                )
            except subprocess.CalledProcessError as e:
                return DeployResult(
                    name, False,
                    f"Local step {i + 1} failed (exit {e.returncode}): {expanded_step}",
                )

    # 3. Run SSH steps
    for i, step in enumerate(steps):
        print(f"[{name}] Step {i + 1}/{len(steps)}: {step}")
        stdout, stderr, returncode = ssh_run(
            host=host, user=user, command=step,
            ssh_key=ssh_key, directory=directory, env=env,
        )
        if returncode != 0:
            return DeployResult(
                name, False,
                f"Step {i + 1} failed (exit {returncode}): {stderr or stdout}",
            )

    # 4. Health check
    health = target_config.get("health")
    if health:
        print(f"[{name}] Running health check...")
        success, message = check_health(health, host, user)
        if not success:
            # Try rollback
            rollback_steps = target_config.get("rollback_steps")
            if rollback_steps:
                print(f"[{name}] Health check failed, running rollback...")
                for j, step in enumerate(rollback_steps):
                    print(f"[{name}] Rollback {j + 1}/{len(rollback_steps)}: {step}")
                    ssh_run(
                        host=host, user=user, command=step,
                        ssh_key=ssh_key, directory=directory, env=env,
                    )
                # Re-check health after rollback
                print(f"[{name}] Re-checking health after rollback...")
                rb_success, _ = check_health(health, host, user)
                if rb_success:
                    return DeployResult(
                        name, False,
                        "Rollback successful, service restored",
                        rolled_back=True,
                    )
                return DeployResult(
                    name, False,
                    "Rollback failed, manual intervention required",
                    rolled_back=True,
                )
            return DeployResult(name, False, message)
        return DeployResult(name, True, message)

    # 5. No health check
    print(
        f"Warning: [{name}] No health check configured, cannot verify deploy",
        file=sys.stderr,
    )
    return DeployResult(name, True, "Deploy completed (no health check)")
