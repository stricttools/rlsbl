"""End-to-end tests for the V5 pre-push hook template.

Verifies that the hook script is syntactically valid bash, correctly sets
RLSBL_PUSH_STDIN from git's piped stdin, and invokes ``rlsbl check --tag prepush``.
"""

import os
import stat
import subprocess
import textwrap

from rlsbl.hook_hashes import CURRENT_PRE_PUSH_HOOK


class TestV5HookIsValidBash:
    """The V5 hook template must be syntactically correct bash."""

    def test_bash_syntax_check(self, tmp_path):
        hook_file = tmp_path / "pre-push"
        hook_file.write_text(CURRENT_PRE_PUSH_HOOK)
        result = subprocess.run(
            ["bash", "-n", str(hook_file)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"


class TestV5HookSetsEnvVar:
    """The hook must capture stdin into RLSBL_PUSH_STDIN before exec-ing."""

    def test_rlsbl_push_stdin_from_piped_input(self, tmp_path):
        """Pipe fake push stdin to the hook and verify RLSBL_PUSH_STDIN is set.

        We replace ``exec rlsbl check --tag prepush`` with a stub that prints
        the env var so we can capture it.
        """
        stdin_data = "refs/heads/main abc123 refs/heads/main def456"

        # Build a modified hook that prints the env var instead of exec-ing rlsbl
        modified_hook = CURRENT_PRE_PUSH_HOOK.replace(
            'exec rlsbl check --tag prepush',
            'echo "$RLSBL_PUSH_STDIN"',
        )
        hook_file = tmp_path / "pre-push"
        hook_file.write_text(modified_hook)
        hook_file.chmod(hook_file.stat().st_mode | stat.S_IEXEC)

        result = subprocess.run(
            ["bash", str(hook_file)],
            input=stdin_data,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr}"
        assert result.stdout.strip() == stdin_data


class TestV5HookInvokesCheckTagPrepush:
    """The hook content must delegate to ``rlsbl check --tag prepush``."""

    def test_hook_contains_check_command(self):
        assert "rlsbl check --tag prepush" in CURRENT_PRE_PUSH_HOOK

    def test_hook_uses_exec(self):
        """The hook should use exec so rlsbl replaces the shell process."""
        assert "exec rlsbl check --tag prepush" in CURRENT_PRE_PUSH_HOOK

    def test_hook_invokes_rlsbl_binary(self, tmp_path):
        """Replace rlsbl with a fake binary and verify it gets called with
        the correct arguments and environment."""
        stdin_data = "refs/heads/main aaa111 refs/heads/main bbb222\n"

        # Create a fake rlsbl that records how it was called
        fake_bin_dir = tmp_path / "bin"
        fake_bin_dir.mkdir()
        fake_rlsbl = fake_bin_dir / "rlsbl"
        fake_rlsbl.write_text(textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "ARGS=$*"
            echo "STDIN_VAR=$RLSBL_PUSH_STDIN"
        """))
        fake_rlsbl.chmod(fake_rlsbl.stat().st_mode | stat.S_IEXEC)

        # Write the real hook
        hook_file = tmp_path / "pre-push"
        hook_file.write_text(CURRENT_PRE_PUSH_HOOK)
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
        lines = result.stdout.strip().splitlines()
        assert "ARGS=check --tag prepush" in lines
        assert f"STDIN_VAR={stdin_data.strip()}" in lines
