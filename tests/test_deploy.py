"""Tests for rlsbl.deploy — config validation, SSH, health checks, and rollback."""

import socket
import subprocess
import urllib.error

import pytest

from rlsbl.deploy import (
    DeployResult,
    check_health,
    deploy_target,
    expand_env_vars,
    ssh_run,
    validate_deploy_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_target(**overrides):
    """Return a minimal valid deploy target dict, with optional overrides."""
    base = {
        "name": "prod",
        "host": "10.0.0.1",
        "steps": ["systemctl restart app"],
        "only_on": ["main"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestValidateDeployConfig:

    def test_valid_config(self):
        targets = [_minimal_target()]
        assert validate_deploy_config(targets) == []

    def test_valid_config_with_all_fields(self):
        targets = [_minimal_target(
            user="deploy",
            ssh_key="/home/deploy/.ssh/id_ed25519",
            directory="/opt/app",
            health={"type": "http", "url": "http://localhost:8080/health"},
            rollback_steps=["systemctl restart app-old"],
            env={"APP_ENV": "production"},
        )]
        assert validate_deploy_config(targets) == []

    def test_missing_required_fields(self):
        # Empty dict is missing all required fields
        errors = validate_deploy_config([{}])
        assert len(errors) == 4
        for field in ("name", "host", "steps", "only_on"):
            assert any(f"'{field}'" in e for e in errors)

    def test_missing_single_field(self):
        target = _minimal_target()
        del target["host"]
        errors = validate_deploy_config([target])
        assert len(errors) == 1
        assert "'host'" in errors[0]

    def test_invalid_only_on_not_list(self):
        errors = validate_deploy_config([_minimal_target(only_on="main")])
        assert len(errors) == 1
        assert "only_on" in errors[0]

    def test_invalid_only_on_empty_list(self):
        errors = validate_deploy_config([_minimal_target(only_on=[])])
        assert len(errors) == 1
        assert "only_on" in errors[0]

    def test_invalid_only_on_non_string_items(self):
        errors = validate_deploy_config([_minimal_target(only_on=[123])])
        assert len(errors) == 1
        assert "only_on" in errors[0]

    def test_invalid_steps_empty(self):
        errors = validate_deploy_config([_minimal_target(steps=[])])
        assert len(errors) == 1
        assert "steps" in errors[0]

    def test_invalid_steps_non_string_items(self):
        errors = validate_deploy_config([_minimal_target(steps=[42])])
        assert len(errors) == 1
        assert "steps" in errors[0]

    def test_duplicate_names(self):
        targets = [_minimal_target(name="web"), _minimal_target(name="web")]
        errors = validate_deploy_config(targets)
        assert len(errors) == 1
        assert "duplicate" in errors[0]

    def test_not_a_list(self):
        errors = validate_deploy_config("not a list")
        assert len(errors) == 1
        assert "must be a list" in errors[0]

    def test_target_not_a_dict(self):
        errors = validate_deploy_config(["not a dict"])
        assert len(errors) == 1
        assert "must be a dict" in errors[0]


class TestExpandEnvVars:

    def test_env_var_expansion(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_HOST", "10.0.0.5")
        assert expand_env_vars("$DEPLOY_HOST") == "10.0.0.5"

    def test_env_var_expansion_inline(self, monkeypatch):
        monkeypatch.setenv("DOMAIN", "example.com")
        assert expand_env_vars("deploy.$DOMAIN") == "deploy.example.com"

    def test_multiple_env_vars(self, monkeypatch):
        monkeypatch.setenv("USER", "admin")
        monkeypatch.setenv("HOST", "server")
        assert expand_env_vars("$USER@$HOST") == "admin@server"

    def test_missing_env_var(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR_12345", raising=False)
        with pytest.raises(ValueError, match="NONEXISTENT_VAR_12345"):
            expand_env_vars("$NONEXISTENT_VAR_12345")

    def test_no_vars_passthrough(self):
        assert expand_env_vars("plain-host") == "plain-host"

    def test_env_var_in_config_validation(self, monkeypatch):
        monkeypatch.setenv("MY_HOST", "10.0.0.1")
        targets = [_minimal_target(host="$MY_HOST")]
        assert validate_deploy_config(targets) == []

    def test_missing_env_var_in_config_validation(self, monkeypatch):
        monkeypatch.delenv("MISSING_HOST_VAR", raising=False)
        targets = [_minimal_target(host="$MISSING_HOST_VAR")]
        errors = validate_deploy_config(targets)
        assert len(errors) == 1
        assert "MISSING_HOST_VAR" in errors[0]


class TestHealthConfigValidation:

    def test_health_http_valid(self):
        targets = [_minimal_target(health={"type": "http", "url": "http://localhost/health"})]
        assert validate_deploy_config(targets) == []

    def test_health_http_missing_url(self):
        targets = [_minimal_target(health={"type": "http"})]
        errors = validate_deploy_config(targets)
        assert len(errors) == 1
        assert "url" in errors[0]

    def test_health_tcp_valid(self):
        targets = [_minimal_target(health={"type": "tcp", "port": 8080})]
        assert validate_deploy_config(targets) == []

    def test_health_tcp_missing_port(self):
        targets = [_minimal_target(health={"type": "tcp"})]
        errors = validate_deploy_config(targets)
        assert len(errors) == 1
        assert "port" in errors[0]

    def test_health_tcp_port_not_int(self):
        targets = [_minimal_target(health={"type": "tcp", "port": "8080"})]
        errors = validate_deploy_config(targets)
        assert len(errors) == 1
        assert "port" in errors[0] and "integer" in errors[0]

    def test_health_script_valid(self):
        targets = [_minimal_target(health={"type": "script", "command": "curl localhost"})]
        assert validate_deploy_config(targets) == []

    def test_health_script_missing_command(self):
        targets = [_minimal_target(health={"type": "script"})]
        errors = validate_deploy_config(targets)
        assert len(errors) == 1
        assert "command" in errors[0]

    def test_health_missing_type(self):
        targets = [_minimal_target(health={"url": "http://localhost"})]
        errors = validate_deploy_config(targets)
        assert len(errors) == 1
        assert "type" in errors[0]

    def test_health_unknown_type(self):
        targets = [_minimal_target(health={"type": "grpc"})]
        errors = validate_deploy_config(targets)
        assert len(errors) == 1
        assert "grpc" in errors[0]

    def test_health_not_a_dict(self):
        targets = [_minimal_target(health="http://localhost")]
        errors = validate_deploy_config(targets)
        assert len(errors) == 1
        assert "must be a dict" in errors[0]


# ---------------------------------------------------------------------------
# SSH execution (mocked)
# ---------------------------------------------------------------------------

class TestSshRun:

    def test_ssh_run_basic(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        stdout, stderr, rc = ssh_run("10.0.0.1", "root", "uptime")
        assert rc == 0
        assert stdout == "ok\n"
        cmd = calls[0]
        assert cmd[0] == "ssh"
        assert "root@10.0.0.1" in cmd
        assert "uptime" in cmd
        assert "-o" in cmd
        assert "BatchMode=yes" in cmd

    def test_ssh_run_with_key(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        ssh_run("10.0.0.1", "deploy", "ls", ssh_key="/home/deploy/.ssh/id_ed25519")
        cmd = calls[0]
        key_idx = cmd.index("-i")
        assert cmd[key_idx + 1] == "/home/deploy/.ssh/id_ed25519"

    def test_ssh_run_with_directory(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        ssh_run("10.0.0.1", "root", "ls", directory="/opt/app")
        # The remote command is the last element
        remote_cmd = calls[0][-1]
        assert "cd /opt/app && ls" in remote_cmd

    def test_ssh_run_with_env(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        ssh_run("10.0.0.1", "root", "echo hi", env={"APP_ENV": "prod", "PORT": "8080"})
        remote_cmd = calls[0][-1]
        assert "export APP_ENV='prod'" in remote_cmd
        assert "export PORT='8080'" in remote_cmd
        assert "echo hi" in remote_cmd

    def test_ssh_run_timeout(self, monkeypatch):
        kwargs_log = []

        def fake_run(cmd, **kwargs):
            kwargs_log.append(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        ssh_run("10.0.0.1", "root", "uptime", timeout=60)
        assert kwargs_log[0]["timeout"] == 60

    def test_ssh_run_failure(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error msg")

        monkeypatch.setattr("subprocess.run", fake_run)

        stdout, stderr, rc = ssh_run("10.0.0.1", "root", "bad-cmd")
        assert rc == 1
        assert stderr == "error msg"


# ---------------------------------------------------------------------------
# Health checks (mocked)
# ---------------------------------------------------------------------------

class TestCheckHealthHttp:

    def test_http_health_success(self, monkeypatch):
        class FakeResp:
            status = 200
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=None: FakeResp())

        success, msg = check_health(
            {"type": "http", "url": "http://localhost:8080/health", "timeout": 5, "interval": 1},
            "10.0.0.1",
        )
        assert success is True
        assert "passed" in msg

    def test_http_health_timeout(self, monkeypatch):
        def failing_urlopen(url, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)
        monkeypatch.setattr("time.monotonic", _monotonic_counter(0, 1, 2, 3, 4, 5))

        success, msg = check_health(
            {"type": "http", "url": "http://localhost:8080/health", "timeout": 3, "interval": 1},
            "10.0.0.1",
        )
        assert success is False
        assert "failed" in msg

    def test_http_health_retry_then_success(self, monkeypatch):
        call_count = [0]

        class FakeResp:
            status = 200
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def urlopen(url, timeout=None):
            call_count[0] += 1
            if call_count[0] < 3:
                raise urllib.error.URLError("not ready")
            return FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", urlopen)

        success, msg = check_health(
            {"type": "http", "url": "http://localhost/health", "timeout": 30, "interval": 0.01},
            "10.0.0.1",
        )
        assert success is True
        assert call_count[0] >= 3


class TestCheckHealthTcp:

    def test_tcp_health_success(self, monkeypatch):
        class FakeConn:
            def close(self): pass

        monkeypatch.setattr("socket.create_connection", lambda addr, timeout=None: FakeConn())

        success, msg = check_health(
            {"type": "tcp", "port": 5432, "timeout": 5},
            "10.0.0.1",
        )
        assert success is True
        assert "passed" in msg

    def test_tcp_health_timeout(self, monkeypatch):
        def failing_connect(addr, timeout=None):
            raise socket.timeout("timed out")

        monkeypatch.setattr("socket.create_connection", failing_connect)
        monkeypatch.setattr("time.monotonic", _monotonic_counter(0, 1, 2, 3, 4, 5))

        success, msg = check_health(
            {"type": "tcp", "port": 5432, "timeout": 3},
            "10.0.0.1",
        )
        assert success is False
        assert "failed" in msg

    def test_tcp_health_uses_config_host(self, monkeypatch):
        connected_to = []

        class FakeConn:
            def close(self): pass

        def capture_connect(addr, timeout=None):
            connected_to.append(addr)
            return FakeConn()

        monkeypatch.setattr("socket.create_connection", capture_connect)

        check_health(
            {"type": "tcp", "host": "db.internal", "port": 5432, "timeout": 5},
            "10.0.0.1",
        )
        assert connected_to[0] == ("db.internal", 5432)


class TestCheckHealthScript:

    def test_script_health_success(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        success, msg = check_health(
            {"type": "script", "command": "curl -f http://localhost/health"},
            "10.0.0.1",
        )
        assert success is True
        assert "passed" in msg

    def test_script_health_failure(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="check failed")

        monkeypatch.setattr("subprocess.run", fake_run)

        success, msg = check_health(
            {"type": "script", "command": "curl -f http://localhost/health"},
            "10.0.0.1",
        )
        assert success is False
        assert "failed" in msg


# ---------------------------------------------------------------------------
# Deploy flow (mocked)
# ---------------------------------------------------------------------------

class TestDeployTarget:

    def test_deploy_branch_restriction(self):
        target = _minimal_target(only_on=["main"])
        result = deploy_target(target, "develop")
        assert result.success is True
        assert "Skipped" in result.message

    def test_deploy_steps_success(self, monkeypatch):
        executed = []

        def fake_run(cmd, **kwargs):
            # Capture the remote command (last element of ssh cmd)
            executed.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        target = _minimal_target(steps=["step1", "step2", "step3"])
        result = deploy_target(target, "main")
        assert result.success is True
        assert len(executed) == 3

    def test_deploy_step_failure(self, monkeypatch):
        executed = []

        def fake_run(cmd, **kwargs):
            executed.append(cmd[-1])
            # Fail on second step
            if len(executed) == 2:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="failed")
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        target = _minimal_target(steps=["step1", "step2", "step3"])
        result = deploy_target(target, "main")
        assert result.success is False
        assert "Step 2 failed" in result.message
        # Step 3 should not have been executed
        assert len(executed) == 2

    def test_deploy_health_check_success(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        class FakeResp:
            status = 200
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=None: FakeResp())

        target = _minimal_target(
            health={"type": "http", "url": "http://localhost/health", "timeout": 5, "interval": 1},
        )
        result = deploy_target(target, "main")
        assert result.success is True
        assert "passed" in result.message

    def test_deploy_health_check_failure_with_rollback(self, monkeypatch):
        executed = []

        def fake_run(cmd, **kwargs):
            executed.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        def failing_urlopen(url, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)
        monkeypatch.setattr("time.monotonic", _monotonic_counter(
            # initial health check (fails after timeout)
            0, 1, 2, 3, 4, 5,
            # post-rollback health check (also fails after timeout)
            6, 7, 8, 9, 10, 11,
        ))

        target = _minimal_target(
            health={"type": "http", "url": "http://localhost/health", "timeout": 2, "interval": 1},
            rollback_steps=["rollback-cmd"],
        )
        result = deploy_target(target, "main")
        assert result.success is False
        assert result.rolled_back is True
        assert "Rollback failed, manual intervention required" in result.message
        # Rollback command should have been executed (after the deploy step)
        assert any("rollback-cmd" in cmd for cmd in executed)

    def test_deploy_no_health_check_warns(self, monkeypatch, capsys):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        target = _minimal_target()
        assert "health" not in target
        result = deploy_target(target, "main")
        assert result.success is True
        assert "no health check" in result.message.lower()
        captured = capsys.readouterr()
        assert "No health check configured" in captured.err

    def test_rollback_rechecks_health_success(self, monkeypatch):
        """After rollback, health re-check passes -> message indicates service restored."""
        executed = []
        health_call_count = [0]

        def fake_run(cmd, **kwargs):
            executed.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        def urlopen_fails_then_succeeds(url, timeout=None):
            health_call_count[0] += 1
            # Initial health check call(s) fail; post-rollback calls succeed
            if health_call_count[0] <= 1:
                raise urllib.error.URLError("Connection refused")
            class FakeResp:
                status = 200
                def read(self): return b"ok"
                def __enter__(self): return self
                def __exit__(self, *a): pass
            return FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", urlopen_fails_then_succeeds)
        monkeypatch.setattr("time.monotonic", _monotonic_counter(
            # initial health check: monotonic(0) sets deadline=2,
            # monotonic(1)<2 enters loop, urlopen fails,
            # monotonic(2) remaining=0 breaks
            0, 1, 2,
            # post-rollback health check: monotonic(3) sets deadline=5,
            # monotonic(4)<5 enters loop, urlopen succeeds
            3, 4,
        ))

        target = _minimal_target(
            health={"type": "http", "url": "http://localhost/health", "timeout": 2, "interval": 1},
            rollback_steps=["rollback-cmd"],
        )
        result = deploy_target(target, "main")
        assert result.success is False
        assert result.rolled_back is True
        assert "Rollback successful, service restored" in result.message
        assert any("rollback-cmd" in cmd for cmd in executed)

    def test_rollback_rechecks_health_failure(self, monkeypatch):
        """After rollback, health re-check fails -> message indicates manual intervention."""
        executed = []

        def fake_run(cmd, **kwargs):
            executed.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        def failing_urlopen(url, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)
        monkeypatch.setattr("time.monotonic", _monotonic_counter(
            # initial health check (fails)
            0, 1, 2, 3, 4, 5,
            # post-rollback health check (also fails)
            6, 7, 8, 9, 10, 11,
        ))

        target = _minimal_target(
            health={"type": "http", "url": "http://localhost/health", "timeout": 2, "interval": 1},
            rollback_steps=["rollback-cmd"],
        )
        result = deploy_target(target, "main")
        assert result.success is False
        assert result.rolled_back is True
        assert "Rollback failed, manual intervention required" in result.message

    def test_script_health_uses_configured_user(self, monkeypatch):
        """Script health check uses the target's configured user, not hardcoded root."""
        ssh_calls = []

        def fake_run(cmd, **kwargs):
            ssh_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        target = _minimal_target(
            user="deploy",
            health={"type": "script", "command": "curl -f http://localhost/health"},
        )
        result = deploy_target(target, "main")
        assert result.success is True

        # Find the health check SSH call (the one running the curl command)
        health_ssh_cmd = None
        for cmd in ssh_calls:
            if any("curl" in part for part in cmd):
                health_ssh_cmd = cmd
                break

        assert health_ssh_cmd is not None, "Health check SSH call not found"
        # Verify the SSH call used user="deploy" not user="root"
        assert "deploy@10.0.0.1" in health_ssh_cmd
        assert "root@10.0.0.1" not in health_ssh_cmd

    def test_deploy_health_failure_no_rollback(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        def failing_urlopen(url, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)
        monkeypatch.setattr("time.monotonic", _monotonic_counter(0, 1, 2, 3, 4, 5))

        target = _minimal_target(
            health={"type": "http", "url": "http://localhost/health", "timeout": 2, "interval": 1},
        )
        result = deploy_target(target, "main")
        assert result.success is False
        assert result.rolled_back is False


class TestDeployResult:

    def test_attributes(self):
        r = DeployResult("web", True, "done")
        assert r.target_name == "web"
        assert r.success is True
        assert r.message == "done"
        assert r.rolled_back is False

    def test_rolled_back(self):
        r = DeployResult("web", False, "health failed", rolled_back=True)
        assert r.rolled_back is True


# ---------------------------------------------------------------------------
# Config integration (read_deploy_config)
# ---------------------------------------------------------------------------

class TestReadDeployConfig:

    def test_returns_empty_when_no_deploy_key(self):
        from rlsbl.config import read_deploy_config

        targets, errors = read_deploy_config({"tag": True})
        assert targets == []
        assert errors == []

    def test_returns_targets_and_errors(self):
        from rlsbl.config import read_deploy_config

        config = {"deploy": [{"name": "prod"}]}  # Missing required fields
        targets, errors = read_deploy_config(config)
        assert len(targets) == 1
        assert len(errors) > 0

    def test_valid_deploy_config(self):
        from rlsbl.config import read_deploy_config

        config = {"deploy": [_minimal_target()]}
        targets, errors = read_deploy_config(config)
        assert len(targets) == 1
        assert errors == []


# ---------------------------------------------------------------------------
# Test utilities
# ---------------------------------------------------------------------------

def _monotonic_counter(*values):
    """Return a fake time.monotonic that returns values from the sequence, then increments."""
    it = iter(values)
    last = [values[-1] if values else 0]

    def fake_monotonic():
        try:
            return next(it)
        except StopIteration:
            last[0] += 1
            return last[0]

    return fake_monotonic
