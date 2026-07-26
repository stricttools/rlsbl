"""Tests for the structural non-local push guard (tests/conftest.py).

The guard makes a real ``git push`` to a non-local remote impossible from the
suite. Forensics: a test that failed to mock ``push_if_needed`` executed a real
``git push origin main`` from the real repo. These tests prove the guard blocks
the non-local case while leaving local-remote pushes (the suite's fixtures)
untouched.
"""

import subprocess

import pytest

from conftest import _remote_is_local, _extract_push_remote


class TestRemoteClassification:
    @pytest.mark.parametrize("url", [
        "/tmp/bare.git",
        "./relative/bare.git",
        "../sibling/bare.git",
        "file:///tmp/bare.git",
        "bare-repo",
    ])
    def test_local_remotes_allowed(self, url):
        assert _remote_is_local(url) is True

    @pytest.mark.parametrize("url", [
        "https://github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "git://example.com/repo.git",
        None,
        "",
    ])
    def test_nonlocal_remotes_blocked(self, url):
        assert _remote_is_local(url) is False

    def test_extract_remote(self):
        assert _extract_push_remote(["git", "push", "origin", "main"]) == "origin"
        assert _extract_push_remote(["git", "push", "-u", "origin", "main"]) == "origin"
        assert _extract_push_remote(["git", "push", "origin", ":v1.0.0"]) == "origin"


class TestGuardKillsNonLocalPush:
    def test_direct_nonlocal_url_push_is_blocked(self, tmp_path):
        """A direct push to an https URL must be killed by the guard."""
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "t@t.local"], cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=str(tmp_path), check=True,
        )
        (tmp_path / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=str(tmp_path), check=True)

        with pytest.raises(BaseException) as exc:
            subprocess.run(
                ["git", "push", "https://github.com/owner/repo.git", "main"],
                cwd=str(tmp_path),
            )
        assert "BLOCKED" in str(exc.value)

    def test_bare_name_nonlocal_origin_is_blocked(self, tmp_path):
        """A push to ``origin`` that resolves to an https URL is blocked."""
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
            cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "t@t.local"], cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=str(tmp_path), check=True,
        )
        (tmp_path / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=str(tmp_path), check=True)

        with pytest.raises(BaseException) as exc:
            subprocess.run(["git", "push", "origin", "main"], cwd=str(tmp_path))
        assert "BLOCKED" in str(exc.value)

    def test_local_bare_repo_push_allowed(self, tmp_path):
        """A push to a local bare repo must proceed normally (not blocked)."""
        bare = tmp_path / "bare.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(bare)], check=True,
        )
        work = tmp_path / "work"
        work.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(work), check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(bare)], cwd=str(work), check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "t@t.local"], cwd=str(work), check=True,
        )
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(work), check=True)
        (work / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=str(work), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=str(work), check=True)

        # Must NOT raise Failed -- local pushes are allowed.
        result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(work), capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


class TestTmpdirSessionGuard:
    """conftest.pytest_configure refuses a temp root inside the repository
    (the Jul junk-commit incidents)."""

    def _fake_config(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            option=SimpleNamespace(basetemp=None),
            addinivalue_line=lambda *a, **k: None,
        )

    def test_basetemp_inside_repo_rejected(self, monkeypatch):
        import conftest
        monkeypatch.delenv("TMPDIR", raising=False)
        cfg = self._fake_config()
        cfg.option.basetemp = str(conftest._REPO_ROOT / "some" / "tmp")
        with pytest.raises(pytest.UsageError, match="inside the repository"):
            conftest.pytest_configure(cfg)

    def test_tmpdir_env_inside_repo_rejected(self, monkeypatch):
        import conftest
        monkeypatch.setenv("TMPDIR", str(conftest._REPO_ROOT / "nested"))
        cfg = self._fake_config()
        with pytest.raises(pytest.UsageError, match="inside the repository"):
            conftest.pytest_configure(cfg)

    def test_tmpdir_outside_repo_accepted(self, monkeypatch, tmp_path):
        import conftest
        # tmp_path is under the pytest basetemp (outside the repo).
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        cfg = self._fake_config()
        # Must NOT raise.
        conftest.pytest_configure(cfg)
