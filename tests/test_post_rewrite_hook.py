"""Tests for the post-rewrite hook: scaffold installation and content."""

import os
import stat
import subprocess

import pytest

from rlsbl.commands.init_cmd import (
    _install_or_update_post_rewrite_hook,
)
from rlsbl.hook_hashes import (
    CURRENT_POST_REWRITE_HOOK,
    CURRENT_POST_REWRITE_HOOK_HASH,
    POST_REWRITE_HOOK_HASHES,
    compute_hook_hash,
)


class TestPostRewriteHookHashInfrastructure:
    """Hash constants and set integrity for the post-rewrite hook."""

    def test_current_hook_in_hash_set(self):
        assert CURRENT_POST_REWRITE_HOOK_HASH in POST_REWRITE_HOOK_HASHES

    def test_current_hash_matches_content(self):
        assert compute_hook_hash(CURRENT_POST_REWRITE_HOOK) == CURRENT_POST_REWRITE_HOOK_HASH


class TestPostRewriteHookContent:
    """The hook content must pipe stdin to ``rlsbl changelog remap --stdin``."""

    def test_hook_pipes_to_changelog_remap(self):
        assert "rlsbl changelog remap --stdin" in CURRENT_POST_REWRITE_HOOK

    def test_hook_uses_exec(self):
        """The hook should use exec so rlsbl replaces the shell process."""
        assert "exec rlsbl changelog remap --stdin" in CURRENT_POST_REWRITE_HOOK

    def test_hook_is_valid_bash(self, tmp_path):
        hook_file = tmp_path / "post-rewrite"
        hook_file.write_text(CURRENT_POST_REWRITE_HOOK)
        result = subprocess.run(
            ["bash", "-n", str(hook_file)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"


class TestScaffoldInstallsPostRewriteHook:
    """Scaffold must install the post-rewrite hook alongside pre-push."""

    def test_install_when_missing(self, mock_git_repo):
        hook = mock_git_repo / ".git" / "hooks" / "post-rewrite"
        assert not hook.exists()

        _install_or_update_post_rewrite_hook()

        assert hook.exists()
        assert hook.read_text() == CURRENT_POST_REWRITE_HOOK
        assert os.access(hook, os.X_OK)

    def test_no_op_when_current(self, mock_git_repo):
        hook = mock_git_repo / ".git" / "hooks" / "post-rewrite"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(CURRENT_POST_REWRITE_HOOK)
        mtime_before = hook.stat().st_mtime

        import time
        time.sleep(0.01)

        _install_or_update_post_rewrite_hook()

        assert hook.read_text() == CURRENT_POST_REWRITE_HOOK
        assert hook.stat().st_mtime == mtime_before

    def test_skip_when_unknown_content(self, mock_git_repo, capsys):
        custom = "#!/usr/bin/env bash\n# user's own post-rewrite logic\necho custom\n"
        assert compute_hook_hash(custom) not in POST_REWRITE_HOOK_HASHES

        hook = mock_git_repo / ".git" / "hooks" / "post-rewrite"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(custom)

        _install_or_update_post_rewrite_hook()

        assert hook.read_text() == custom
        captured = capsys.readouterr()
        assert "appears customized" in captured.err

    def test_no_git_dir_no_op(self, tmp_project):
        assert not os.path.isdir(".git")
        _install_or_update_post_rewrite_hook()
        assert not os.path.exists(".git/hooks/post-rewrite")


class TestPostRewriteHookPipesToRemap:
    """End-to-end: the hook script pipes git's stdin to rlsbl changelog remap."""

    def test_hook_invokes_remap_with_stdin(self, tmp_path):
        """Replace rlsbl with a fake that records args and stdin."""
        stdin_data = "old_abc123 new_def456\nold_111aaa new_222bbb\n"

        # Create a fake rlsbl that records how it was called
        fake_bin_dir = tmp_path / "bin"
        fake_bin_dir.mkdir()
        fake_rlsbl = fake_bin_dir / "rlsbl"
        fake_rlsbl.write_text(
            '#!/usr/bin/env bash\n'
            'echo "ARGS=$*"\n'
            'echo "STDIN=$(cat)"\n'
        )
        fake_rlsbl.chmod(fake_rlsbl.stat().st_mode | stat.S_IEXEC)

        # Write the real hook
        hook_file = tmp_path / "post-rewrite"
        hook_file.write_text(CURRENT_POST_REWRITE_HOOK)
        hook_file.chmod(hook_file.stat().st_mode | stat.S_IEXEC)

        # Run with fake rlsbl on PATH
        env = os.environ.copy()
        env["PATH"] = str(fake_bin_dir) + os.pathsep + env.get("PATH", "")

        result = subprocess.run(
            ["bash", str(hook_file)],
            input=stdin_data,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr}"
        output = result.stdout.strip()
        assert "ARGS=changelog remap --stdin" in output
        # stdin_data is piped through -- verify both mapping lines appear
        assert "old_abc123 new_def456" in output
        assert "old_111aaa new_222bbb" in output
