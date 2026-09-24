"""In-consumer proof that the stricttest floor is armed in THIS suite.

rlsbl distributes the floor's outer layer (the bwrap runner template and the
``stricttest-floor`` adoption check), so it runs the published plugin instead of
a private copy of it -- `tests/conftest.py` used to carry all of this verbatim.
The plugin ships its own meta-tests, but those prove the guards work in the
*plugin's* repo. What they cannot prove is that rlsbl's declared stance is the
one that actually binds here: an accidental edit to
``[tool.pytest.ini_options]`` (or a dropped dependency) would silently unarm the
floor and no plugin-side test would notice.

So each layer gets a deliberate probe, run in-process, against the real resolved
settings:

* the env-poisoning floor actually repointed HOME and stripped credentials,
* the push guard intercepts a real ``git push`` to a non-local remote,
* the bare-run threshold refuses an over-threshold count outside the sandbox,
* the TMPDIR-inside-repo refusal fires for a temp root under the repository,
* the socket guard (net-new -- rlsbl's old floor had no in-process network
  guard) refuses egress, loopback included,
* and the resolved settings match what pyproject.toml declares.
"""

import os
import socket
import subprocess
from pathlib import Path

import pytest
from _pytest.outcomes import Failed

from stricttest import config as st_config
from stricttest import envfloor, sandbox, socketguard
from stricttest.plugin import settings as resolved_settings

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPluginIsTheSource:
    """The floor comes from the installed plugin, not from a private copy."""

    def test_plugin_is_registered(self, request):
        assert request.config.pluginmanager.hasplugin("stricttest"), (
            "the stricttest pytest11 plugin is not loaded -- the floor this "
            "suite relies on would be absent"
        )

    def test_conftest_does_not_reimplement_the_floor(self):
        """A re-introduced private copy would diverge from what we distribute."""
        text = (_REPO_ROOT / "tests" / "conftest.py").read_text()
        for banned in (
            "_install_env_poisoning_floor",
            "_enforce_sandbox_threshold",
            "def _chdir_into_tmp",
            "def _guard_nonlocal_push",
        ):
            assert banned not in text, (
                f"tests/conftest.py re-implements {banned!r}, which the "
                "stricttest plugin already provides. Configure the plugin via "
                "[tool.pytest.ini_options] instead of forking the floor."
            )


class TestDeclaredStance:
    """pyproject.toml's declaration is what actually binds."""

    def test_settings_match_the_declaration(self):
        s = resolved_settings()
        assert s.sockets == "deny"
        assert s.socket_allowlist == ()
        assert s.unix_socket_allowlist == ()
        assert s.loopback == "deny"
        assert s.sandbox_required is True
        assert s.threshold == 50
        assert s.tmp_prefix == "rlsbl-test-env-"
        assert s.git_user_name == "rlsbl-test"
        assert s.git_user_email == "rlsbl-test@example.invalid"
        assert s.preserve == (
            "go_path", "go_mod_cache", "go_cache", "python_user_base",
        )

    def test_sandbox_handshake_var_is_the_plugin_default(self):
        """scripts/test.sh and the floor must agree on ONE variable name."""
        s = resolved_settings()
        assert s.sandbox_env == "STRICTTEST_SANDBOX"
        runner = (_REPO_ROOT / "scripts" / "test.sh").read_text()
        assert f"--setenv {s.sandbox_env} 1" in runner, (
            "the sandbox runner does not export the variable the floor reads; "
            "the bare-run refusal would fire inside the sandbox"
        )

    def test_runner_command_names_the_real_runner(self):
        s = resolved_settings()
        assert (_REPO_ROOT / s.runner_command).is_file()


class TestEnvPoisoningFloor:
    """Probe: the real developer environment is unreachable from a test."""

    def test_home_is_the_throwaway_one(self):
        session_dir = envfloor.session_env_dir()
        assert session_dir is not None, "the env floor was never installed"
        assert session_dir.name.startswith("rlsbl-test-env-")
        home = Path(os.environ["HOME"]).resolve()
        assert home == (session_dir / "home").resolve()
        assert not (home / ".ssh").exists()
        assert not (home / ".gitconfig").exists()

    def test_git_config_is_the_throwaway_one(self):
        session_dir = envfloor.session_env_dir()
        gitconfig = Path(os.environ["GIT_CONFIG_GLOBAL"])
        assert gitconfig == session_dir / "gitconfig"
        assert os.environ["GIT_CONFIG_SYSTEM"] == str(gitconfig)
        body = gitconfig.read_text()
        assert "rlsbl-test@example.invalid" in body
        assert "allow = never" in body

    @pytest.mark.parametrize("var", envfloor.CREDENTIAL_VARS)
    def test_credential_vector_is_stripped(self, var):
        assert var not in os.environ

    def test_git_transport_is_locked_down(self):
        assert os.environ["GIT_ALLOW_PROTOCOL"] == "file"
        assert os.environ["GIT_SSH_COMMAND"] == "/bin/false"
        assert os.environ["GIT_PROXY_COMMAND"] == "/bin/false"
        assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
        assert os.environ["GIT_ASKPASS"] == "/bin/false"

    def test_toolchain_caches_survived_the_home_repoint(self):
        """The declared preserve set is why real-git/go fixtures still work."""
        home = os.environ["HOME"]
        for var in ("GOPATH", "GOMODCACHE", "GOCACHE", "PYTHONUSERBASE"):
            value = os.environ.get(var)
            assert value, f"{var} was not preserved across the HOME repoint"
            assert not value.startswith(home), (
                f"{var} points inside the throwaway HOME ({value}) -- the "
                "toolchain would rebuild from cold on every session"
            )


class TestPushGuard:
    """Probe: a real ``git push`` to a non-local remote is intercepted."""

    def _repo_with_remote(self, tmp_path, url):
        repo = tmp_path / "repo"
        repo.mkdir()
        env = {k: v for k, v in os.environ.items()}
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "remote", "add", "origin", url],
        ):
            subprocess.run(cmd, cwd=str(repo), check=True,
                           capture_output=True, text=True, env=env)
        (repo / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(repo),
                       check=True, capture_output=True, text=True)
        return repo

    def test_push_to_https_remote_is_blocked(self, tmp_path):
        repo = self._repo_with_remote(tmp_path,
                                      "https://github.com/smm-h/rlsbl.git")
        with pytest.raises(Failed) as exc:
            subprocess.run(["git", "push", "origin", "main"], cwd=str(repo),
                           capture_output=True, text=True)
        assert "BLOCKED" in str(exc.value)
        assert "non-local remote" in str(exc.value)

    def test_push_to_a_direct_url_argument_is_blocked(self, tmp_path):
        """No named remote at all: the URL is the push argument itself."""
        repo = self._repo_with_remote(tmp_path, "https://example.com/x.git")
        with pytest.raises(Failed) as exc:
            subprocess.run(
                ["git", "push", "https://github.com/smm-h/rlsbl.git", "main"],
                cwd=str(repo), capture_output=True, text=True,
            )
        assert "BLOCKED" in str(exc.value)

    def test_push_to_scp_style_remote_is_blocked(self, tmp_path):
        repo = self._repo_with_remote(tmp_path, "git@github.com:smm-h/rlsbl.git")
        with pytest.raises(Failed):
            subprocess.run(["git", "push", "origin", "main"], cwd=str(repo),
                           capture_output=True, text=True)

    def test_push_to_a_local_bare_remote_is_allowed(self, tmp_path):
        """The guard must not break the suite's own local-remote fixtures."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "-q", "--bare"], cwd=str(bare),
                       check=True, capture_output=True, text=True)
        repo = self._repo_with_remote(tmp_path, str(bare))
        result = subprocess.run(["git", "push", "-q", "origin", "main"],
                                cwd=str(repo), capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


class TestBareRunThreshold:
    """Probe: an over-threshold bare run is refused."""

    def test_over_threshold_outside_the_sandbox_is_refused(self, monkeypatch):
        s = resolved_settings()
        monkeypatch.delenv(s.sandbox_env, raising=False)
        with pytest.raises(pytest.UsageError) as exc:
            sandbox.enforce_threshold(s, s.threshold + 1)
        message = str(exc.value)
        assert f"Refusing to run {s.threshold + 1} tests bare" in message
        assert s.runner_command in message

    def test_at_threshold_stays_bare_runnable(self, monkeypatch):
        s = resolved_settings()
        monkeypatch.delenv(s.sandbox_env, raising=False)
        sandbox.enforce_threshold(s, s.threshold)

    def test_inside_the_sandbox_any_count_is_allowed(self, monkeypatch):
        s = resolved_settings()
        monkeypatch.setenv(s.sandbox_env, "1")
        sandbox.enforce_threshold(s, s.threshold * 1000)


class TestTmpdirRefusal:
    """Probe: a temp root inside the repository is refused at startup."""

    def test_basetemp_inside_the_repo_is_refused(self):
        with pytest.raises(pytest.UsageError) as exc:
            sandbox.enforce_tmp_outside_repo(_REPO_ROOT, _REPO_ROOT / "tmp")
        assert "inside the repository" in str(exc.value)

    def test_tmpdir_env_inside_the_repo_is_refused(self, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(_REPO_ROOT / "scratch"))
        with pytest.raises(pytest.UsageError):
            sandbox.enforce_tmp_outside_repo(_REPO_ROOT, None)

    def test_the_live_tmpdir_is_outside_the_repo(self):
        """The refusal is not merely available -- this session satisfies it."""
        sandbox.enforce_tmp_outside_repo(_REPO_ROOT, None)


class TestSocketGuard:
    """Probe: in-process network egress is refused under the declared stance."""

    def test_guard_is_armed_with_the_declared_policy(self):
        policy = socketguard.current_policy()
        assert policy is not None, "the socket guard was never armed"
        assert policy.sockets == "deny"
        assert policy.loopback == "deny"

    def test_name_resolution_is_refused(self):
        with pytest.raises(socketguard.NetworkBlocked) as exc:
            socket.getaddrinfo("pypi.org", 443)
        assert "BLOCKED" in str(exc.value)

    def test_direct_connect_is_refused(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(socketguard.NetworkBlocked):
                sock.connect(("93.184.216.34", 80))
        finally:
            sock.close()

    def test_loopback_is_refused_under_the_deny_stance(self):
        with pytest.raises(socketguard.NetworkBlocked):
            socket.create_connection(("127.0.0.1", 9), timeout=1)


class TestConfigKeysAreDeclared:
    """Every REQUIRED safety key is present in rlsbl's own pyproject.toml."""

    def test_no_required_key_is_missing(self, request):
        assert st_config.missing_required_keys(request.config) == []
