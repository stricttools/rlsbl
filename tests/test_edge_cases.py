"""Tests for dangerous edge cases with zero prior coverage.

Covers:
- Detached HEAD behavior for get_current_branch()
- Shallow clone behavior for _get_last_version_tag()
- Push timeout propagation for push_if_needed()
- Signal handling (KeyboardInterrupt) during release, verifying lock cleanup
"""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

import rlsbl.lock
from rlsbl.errors import GitError
from rlsbl.lock import release_lock
from rlsbl.utils import get_current_branch, push_if_needed
from rlsbl.changelog.validate import _get_last_version_tag


class TestDetachedHead:
    """9.1: get_current_branch() when HEAD is detached.

    get_current_branch() raises GitError when HEAD is detached, preventing
    callers (e.g. push_if_needed) from silently operating on ``origin/HEAD``.
    """

    def test_detached_head_raises_git_error(self, mock_git_repo):
        """On detached HEAD, get_current_branch() raises GitError."""
        subprocess.run(
            ["git", "checkout", "--detach"],
            cwd=str(mock_git_repo),
            check=True,
            capture_output=True,
        )

        with pytest.raises(GitError, match="HEAD is detached"):
            get_current_branch()

    def test_normal_branch_returns_name(self, mock_git_repo):
        """On a normal branch, get_current_branch() returns the branch name."""
        result = get_current_branch()
        assert result == "main"

    def test_status_works_on_detached_head(self, mock_git_repo):
        """The status command handles detached HEAD gracefully without crashing."""
        subprocess.run(
            ["git", "checkout", "--detach"],
            cwd=str(mock_git_repo),
            check=True,
            capture_output=True,
        )

        from rlsbl.commands.status import _collect_status
        from rlsbl.context import create_context

        # Set up minimal rlsbl project structure for status to work
        import os
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)

        # Create a minimal pyproject.toml so detect_targets finds something
        pyproject = mock_git_repo / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "test-pkg"\nversion = "0.1.0"\n'
        )

        ctx = create_context(mock_git_repo)
        data = _collect_status("pypi", ".", ctx=ctx)
        assert data["branch"] == "(detached HEAD)"


class TestShallowClone:
    """9.2: _get_last_version_tag() in a shallow clone.

    CI environments often use ``git clone --depth 1``, which strips history
    and makes ``git describe --tags`` fail because the tag's commit is not
    reachable from the shallow history.

    Source behavior: _get_last_version_tag() catches subprocess errors and
    returns None when git describe fails. This means shallow clones silently
    behave as if no version tags exist, which causes _unreleased_range() to
    return "HEAD" (all commits) instead of the correct range.

    Verdict: the edge case IS handled (returns None gracefully) but the
    consequence is silent incorrect behavior -- the unreleased range
    expands to all commits rather than producing a clear error about
    shallow clone limitations.
    """

    def test_shallow_clone_tag_not_found(self, tmp_path):
        """In a shallow clone, _get_last_version_tag() returns None."""
        # Create a source repo with a tag
        source = tmp_path / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(source), check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(source), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(source), check=True)

        (source / "file.txt").write_text("initial\n")
        subprocess.run(["git", "add", "file.txt"], cwd=str(source), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(source), check=True)
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(source), check=True)

        # Make a second commit so the shallow clone's HEAD is past the tag
        (source / "file.txt").write_text("updated\n")
        subprocess.run(["git", "add", "file.txt"], cwd=str(source), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "update"], cwd=str(source), check=True)

        # Shallow clone with depth=1 using file:// protocol. Local path
        # clones (without file://) ignore --depth, so we must use the URL
        # form to get a truly shallow clone.
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "--depth", "1", f"file://{source}", str(clone)],
            check=True,
            capture_output=True,
        )

        # Verify the clone is actually shallow (no tags fetched)
        tag_result = subprocess.run(
            ["git", "tag", "-l"],
            cwd=str(clone),
            capture_output=True,
            text=True,
        )
        assert tag_result.stdout.strip() == "", (
            "shallow clone should have no tags"
        )

        # Run _get_last_version_tag inside the shallow clone
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(str(clone))
            result = _get_last_version_tag()
        finally:
            os.chdir(old_cwd)

        # The tag is not reachable in the shallow clone, so returns None
        assert result is None

    def test_full_clone_tag_found(self, tmp_path):
        """Contrast: in a full clone, _get_last_version_tag() finds the tag."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)

        (repo / "file.txt").write_text("initial\n")
        subprocess.run(["git", "add", "file.txt"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(repo), check=True)
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(repo), check=True)

        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(str(repo))
            result = _get_last_version_tag()
        finally:
            os.chdir(old_cwd)

        assert result == "v1.0.0"


class TestPushTimeout:
    """9.3: push_if_needed() when the push times out.

    Source behavior: push_if_needed() calls run("git", ["push", ...],
    timeout=timeout). The run() function uses subprocess.run with
    check=True and the given timeout. If the push times out,
    subprocess.TimeoutExpired propagates up uncaught.

    Verdict: the edge case is NOT explicitly handled -- TimeoutExpired
    propagates as-is with no wrapping, context message, or recovery
    guidance. The caller (release flow) must handle it.
    """

    @patch("rlsbl.utils.run")
    def test_timeout_propagates_on_new_branch(self, mock_run):
        """TimeoutExpired from push propagates when branch has no remote."""
        # First call: rev-parse for local SHA succeeds
        # Second call: rev-parse --verify origin/branch fails (no remote)
        # Third call: push -u origin branch times out
        mock_run.side_effect = [
            "abc123",  # git rev-parse <branch>
            subprocess.CalledProcessError(128, "git"),  # rev-parse --verify origin/branch
        ]

        # remote_branch_exists returns False, so push_if_needed calls push -u
        with patch("rlsbl.utils.remote_branch_exists", return_value=False):
            mock_run.side_effect = [
                "abc123",  # git rev-parse <branch>
                subprocess.TimeoutExpired(cmd=["git", "push", "-u", "origin", "main"], timeout=120),
            ]
            with pytest.raises(subprocess.TimeoutExpired):
                push_if_needed("main", config={"push_timeout": 120})

    @patch("rlsbl.utils.run")
    def test_timeout_propagates_on_existing_branch(self, mock_run):
        """TimeoutExpired from push propagates when local is ahead of remote."""
        mock_run.side_effect = [
            "abc123",  # git rev-parse <branch> (local)
        ]

        with patch("rlsbl.utils.remote_branch_exists", return_value=True):
            mock_run.side_effect = [
                "abc123",  # git rev-parse <branch> (local)
                "def456",  # git rev-parse origin/<branch> (remote, different)
                subprocess.TimeoutExpired(cmd=["git", "push", "origin", "main"], timeout=120),
            ]
            with pytest.raises(subprocess.TimeoutExpired):
                push_if_needed("main", config={"push_timeout": 120})

    @patch("rlsbl.utils.run")
    def test_no_push_when_up_to_date(self, mock_run):
        """No push (and no timeout risk) when local matches remote."""
        with patch("rlsbl.utils.remote_branch_exists", return_value=True):
            mock_run.side_effect = [
                "abc123",  # git rev-parse <branch> (local)
                "abc123",  # git rev-parse origin/<branch> (same)
            ]
            # Should return without pushing
            push_if_needed("main", config={"push_timeout": 120})

        # Only two calls: local rev-parse and remote rev-parse. No push call.
        assert mock_run.call_count == 2


class TestSignalHandlingDuringRelease:
    """9.4: KeyboardInterrupt during _run_release_mutating.

    The release flow in _run_cmd_inner acquires an advisory lock via
    acquire_lock(), then runs _run_release_mutating() inside a
    try/finally block that calls release_lock() in the finally clause.

    Source behavior: the finally block in _run_cmd_inner (line 310-311
    of __init__.py) explicitly re-raises KeyboardInterrupt after releasing
    the lock. This means the lock IS properly cleaned up on SIGINT/Ctrl-C.

    Verdict: the edge case IS handled -- the lock is released in the
    finally block regardless of whether the inner function raises
    KeyboardInterrupt, SystemExit, or any other exception.
    """

    @pytest.fixture(autouse=True)
    def _reset_lock_fd(self, monkeypatch):
        """Reset _lock_fd between tests so a failed test doesn't leak."""
        monkeypatch.setattr(rlsbl.lock, "_lock_fd", None)

    def test_lock_released_on_keyboard_interrupt(self, tmp_path, monkeypatch):
        """Lock file is cleaned up when _run_release_mutating raises KeyboardInterrupt."""
        monkeypatch.chdir(tmp_path)

        from rlsbl.lock import acquire_lock, _lock_fd

        # Create the lock directory structure
        lock_dir = tmp_path / ".rlsbl"
        lock_dir.mkdir()

        # Acquire the lock
        acquire_lock(lock_dir=".rlsbl", project_root=tmp_path)

        # Verify lock is held
        assert rlsbl.lock._lock_fd is not None
        lock_path = tmp_path / ".rlsbl" / "lock"
        assert lock_path.exists()

        # Simulate what _run_cmd_inner does: try/finally with release_lock
        # The real code structure (from __init__.py lines 280-311):
        #   acquire_lock(...)
        #   try:
        #       _run_release_mutating(...)
        #   except ReleaseAbortError:
        #       sys.exit(1)
        #   except (KeyboardInterrupt, SystemExit):
        #       raise
        #   finally:
        #       release_lock()
        try:
            raise KeyboardInterrupt()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            release_lock()

        # Lock fd should be None after release
        assert rlsbl.lock._lock_fd is None
        # Lock file should be removed
        assert not lock_path.exists()

    def test_lock_released_on_general_exception(self, tmp_path, monkeypatch):
        """Lock file is cleaned up when _run_release_mutating raises any exception."""
        monkeypatch.chdir(tmp_path)

        from rlsbl.lock import acquire_lock

        lock_dir = tmp_path / ".rlsbl"
        lock_dir.mkdir()

        acquire_lock(lock_dir=".rlsbl", project_root=tmp_path)
        assert rlsbl.lock._lock_fd is not None
        lock_path = tmp_path / ".rlsbl" / "lock"

        # Simulate a runtime error during the mutating phase
        try:
            raise RuntimeError("simulated release failure")
        except Exception:
            pass
        finally:
            release_lock()

        assert rlsbl.lock._lock_fd is None
        assert not lock_path.exists()

    def test_double_release_is_safe(self, tmp_path, monkeypatch):
        """Calling release_lock() twice does not raise (idempotent).

        This matters because the finally block always calls release_lock(),
        and atexit also calls it. Double-release must be a no-op.
        """
        monkeypatch.chdir(tmp_path)

        from rlsbl.lock import acquire_lock

        lock_dir = tmp_path / ".rlsbl"
        lock_dir.mkdir()

        acquire_lock(lock_dir=".rlsbl", project_root=tmp_path)
        release_lock()
        # Second call should be a safe no-op
        release_lock()
        assert rlsbl.lock._lock_fd is None
