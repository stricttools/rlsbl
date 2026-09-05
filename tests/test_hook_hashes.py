"""Tests for rlsbl.hook_hashes and the content-hash-based pre-push regeneration."""

import os


from rlsbl.commands.init_cmd import _install_or_update_pre_push_hook
from rlsbl.hook_hashes import (
    CURRENT_PRE_PUSH_HOOK,
    CURRENT_PRE_PUSH_HOOK_HASH,
    PRE_PUSH_HOOK_HASHES,
    compute_hook_hash,
)


class TestComputeHookHash:
    """Pure-function tests for compute_hook_hash."""

    def test_stable_same_content_same_hash(self):
        content = "#!/usr/bin/env bash\necho hi\n"
        assert compute_hook_hash(content) == compute_hook_hash(content)

    def test_trailing_whitespace_ignored(self):
        a = "#!/usr/bin/env bash\necho hi\n"
        b = "#!/usr/bin/env bash\necho hi\n\n\n  \t  "
        assert compute_hook_hash(a) == compute_hook_hash(b)

    def test_different_content_different_hash(self):
        a = "#!/usr/bin/env bash\necho hi\n"
        b = "#!/usr/bin/env bash\necho bye\n"
        assert compute_hook_hash(a) != compute_hook_hash(b)

    def test_accepts_bytes(self):
        content = "#!/usr/bin/env bash\necho hi\n"
        assert compute_hook_hash(content) == compute_hook_hash(content.encode("utf-8"))


class TestHashSet:
    """Tests on the PRE_PUSH_HOOK_HASHES set itself."""

    def test_current_hook_in_set(self):
        assert CURRENT_PRE_PUSH_HOOK_HASH in PRE_PUSH_HOOK_HASHES

    def test_current_hash_matches_template(self):
        assert compute_hook_hash(CURRENT_PRE_PUSH_HOOK) == CURRENT_PRE_PUSH_HOOK_HASH

    def test_at_least_four_versions(self):
        """We track at least four historical versions."""
        assert len(PRE_PUSH_HOOK_HASHES) >= 4


class TestInstallOrUpdate:
    """Tests for the regeneration logic in _install_or_update_pre_push_hook."""

    def test_install_when_missing(self, mock_git_repo):
        """If .git/hooks/pre-push doesn't exist, install the current template."""
        hook = mock_git_repo / ".git" / "hooks" / "pre-push"
        assert not hook.exists()

        _install_or_update_pre_push_hook()

        assert hook.exists()
        assert hook.read_text() == CURRENT_PRE_PUSH_HOOK
        # Verify chmod 755 (at least executable)
        assert os.access(hook, os.X_OK)

    def test_no_op_when_current(self, mock_git_repo):
        """If hook content matches the current template, do nothing."""
        hook = mock_git_repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(CURRENT_PRE_PUSH_HOOK)
        mtime_before = hook.stat().st_mtime

        import time
        time.sleep(0.01)

        _install_or_update_pre_push_hook()

        # File still has the current content
        assert hook.read_text() == CURRENT_PRE_PUSH_HOOK
        # mtime unchanged -- no write occurred
        assert hook.stat().st_mtime == mtime_before

    def test_upgrade_when_known_old_hash(self, mock_git_repo, capsys):
        """If hook content matches a known historical hash, overwrite with current."""
        # Use a known historical version (V3 with $@)
        v3_content = '#!/usr/bin/env bash\nexec rlsbl pre-push-check "$@"\n'
        assert compute_hook_hash(v3_content) in PRE_PUSH_HOOK_HASHES

        hook = mock_git_repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(v3_content)

        _install_or_update_pre_push_hook()

        assert hook.read_text() == CURRENT_PRE_PUSH_HOOK
        out = capsys.readouterr().out
        assert "Updated pre-push hook" in out

    def test_skip_when_unknown_content(self, mock_git_repo, capsys):
        """If hook content hash is unknown, leave it alone and print a diff."""
        custom = "#!/usr/bin/env bash\n# this is a user customization\necho doing my thing\n"
        assert compute_hook_hash(custom) not in PRE_PUSH_HOOK_HASHES

        hook = mock_git_repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(custom)

        _install_or_update_pre_push_hook()

        # File untouched
        assert hook.read_text() == custom

        captured = capsys.readouterr()
        # Diff printed on stderr
        assert "appears customized" in captured.err
        assert "delete the hook" in captured.err
        # Unified diff includes both fromfile and tofile labels
        assert "rlsbl template" in captured.err

    def test_no_git_dir_no_op(self, tmp_project):
        """Outside a git repo (no .git dir) the helper does nothing."""
        # tmp_project has no .git
        assert not os.path.isdir(".git")

        # Should not raise
        _install_or_update_pre_push_hook()

        # No hook installed
        assert not os.path.exists(".git/hooks/pre-push")
