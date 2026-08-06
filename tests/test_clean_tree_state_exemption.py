"""rlsbl's own release state must not block its own clean-tree check.

`rlsbl release run` writes `.rlsbl/releases/in-progress.json` (and
`scrub-result.json`) as untracked, non-gitignored files.  The clean-tree
gate then saw them as uncommitted work and refused to start or resume --
the tool blocking itself.  Observed live twice.

The exemption is structural: `validate_clean_tree` path-matches the two
tool-owned state filenames under their two canonical homes, so a stale
consumer `.gitignore` cannot defeat it.  It must NOT swallow genuine dirt,
and must NOT cover `unreleased.plan.json`, which is deliberately committed.
"""

import os
import subprocess

import pytest

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    is_tool_owned_state_path,
    validate_clean_tree,
)


STATE_REL = os.path.join(".rlsbl", "releases", "in-progress.json")
SCRUB_REL = os.path.join(".rlsbl", "releases", "scrub-result.json")
RELEASABLE_STATE_REL = os.path.join(
    ".rlsbl-monorepo", "releasables", "core", "releases", "in-progress.json"
)
RELEASABLE_SCRUB_REL = os.path.join(
    ".rlsbl-monorepo", "releasables", "core", "releases", "scrub-result.json"
)


def _write(repo, rel_path, content="{}\n"):
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestToolOwnedStatePathPredicate:
    """Unit coverage for the structural path match."""

    @pytest.mark.parametrize("path", [
        ".rlsbl/releases/in-progress.json",
        ".rlsbl/releases/scrub-result.json",
        ".rlsbl-monorepo/releasables/core/releases/in-progress.json",
        ".rlsbl-monorepo/releasables/core/releases/scrub-result.json",
        # Implicit-monorepo member: git status reports repo-root-relative paths.
        "packages/core/.rlsbl/releases/in-progress.json",
    ])
    def test_tool_owned_paths_match(self, path):
        assert is_tool_owned_state_path(path) is True

    @pytest.mark.parametrize("path", [
        # Deliberately committed -- never exempt.
        ".rlsbl/releases/unreleased.plan.json",
        ".rlsbl-monorepo/releasables/core/releases/unreleased.plan.json",
        ".rlsbl/releases/unreleased.toml",
        # Right filename, wrong home.
        "in-progress.json",
        "src/in-progress.json",
        ".rlsbl/in-progress.json",
        ".rlsbl/changes/in-progress.json",
        ".rlsbl-monorepo/releases/in-progress.json",
        # Ordinary work.
        "notes.txt",
        "rlsbl/commands/release/validate.py",
    ])
    def test_other_paths_do_not_match(self, path):
        assert is_tool_owned_state_path(path) is False

    def test_directory_entry_is_not_exempt(self):
        """git may report a new dir as `?? .rlsbl/releases/` -- not a state file."""
        assert is_tool_owned_state_path(".rlsbl/releases/") is False


class TestValidateCleanTreeStateExemption:
    """Integration: a real repo, real `git status --porcelain`."""

    def test_clean_repo_passes(self, mock_git_repo):
        assert validate_clean_tree({}) == set()

    @pytest.mark.parametrize("rel", [
        STATE_REL, SCRUB_REL, RELEASABLE_STATE_REL, RELEASABLE_SCRUB_REL,
    ])
    def test_untracked_tool_state_does_not_block(self, mock_git_repo, rel):
        """The defect: rlsbl's own state file made rlsbl refuse to run."""
        _write(mock_git_repo, rel)
        assert validate_clean_tree({}) == set()

    def test_tracked_but_modified_tool_state_does_not_block(self, mock_git_repo):
        """A repo that committed its state file once is exempt too."""
        _write(mock_git_repo, STATE_REL)
        subprocess.run(["git", "add", STATE_REL], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "state"], cwd=str(mock_git_repo), check=True,
        )
        _write(mock_git_repo, STATE_REL, '{"completed_steps": ["COMMITTED"]}\n')
        assert validate_clean_tree({}) == set()

    def test_genuine_dirt_still_blocks_and_names_only_the_real_file(self, mock_git_repo):
        """The exemption must not swallow a genuinely dirty tree."""
        _write(mock_git_repo, STATE_REL)
        _write(mock_git_repo, SCRUB_REL)
        _write(mock_git_repo, "notes.txt", "wip\n")

        with pytest.raises(ReleaseValidationError) as exc:
            validate_clean_tree({})

        message = str(exc.value)
        assert "notes.txt" in message
        assert "in-progress.json" not in message
        assert "scrub-result.json" not in message

    def test_modified_tracked_file_still_blocks(self, mock_git_repo):
        _write(mock_git_repo, STATE_REL)
        (mock_git_repo / "README.md").write_text("# test\nedited\n")

        with pytest.raises(ReleaseValidationError) as exc:
            validate_clean_tree({})
        assert "README.md" in str(exc.value)

    def test_unreleased_plan_json_still_blocks(self, mock_git_repo):
        """unreleased.plan.json is deliberately committed -- never exempt."""
        _write(mock_git_repo, os.path.join(".rlsbl", "releases", "unreleased.plan.json"))

        with pytest.raises(ReleaseValidationError) as exc:
            validate_clean_tree({})
        assert "unreleased.plan.json" in str(exc.value)

    def test_allow_dirty_still_reports_state_files_as_baseline(self, mock_git_repo):
        """--allow-dirty keeps returning every dirty path, exempt ones included.

        The returned set is the baseline the unexpected-files guard subtracts
        later; dropping the state file from it would make the guard flag it.
        """
        # Track the releases dir first so git reports the state file
        # individually instead of collapsing it into `?? .rlsbl/releases/`.
        _write(mock_git_repo, os.path.join(".rlsbl", "releases", "unreleased.toml"), "")
        subprocess.run(
            ["git", "add", os.path.join(".rlsbl", "releases", "unreleased.toml")],
            cwd=str(mock_git_repo), check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "release file"],
            cwd=str(mock_git_repo), check=True,
        )
        _write(mock_git_repo, STATE_REL)
        _write(mock_git_repo, "notes.txt", "wip\n")

        dirty = validate_clean_tree({"allow-dirty": True})
        assert "notes.txt" in dirty
        assert STATE_REL.replace(os.sep, "/") in dirty

    def test_wholly_untracked_releases_dir_still_blocks_on_real_dirt(self, mock_git_repo):
        """`?? .rlsbl/releases/` must be classified per-file, not exempted wholesale.

        git collapses a wholly untracked directory into one entry. The check
        must still see the deliberately-committed unreleased.toml inside it.
        """
        _write(mock_git_repo, STATE_REL)
        _write(mock_git_repo, os.path.join(".rlsbl", "releases", "unreleased.toml"), "")

        with pytest.raises(ReleaseValidationError) as exc:
            validate_clean_tree({})
        message = str(exc.value)
        assert "unreleased.toml" in message
        assert "in-progress.json" not in message

    def test_unreadable_status_fails_closed(self, mock_git_repo, monkeypatch):
        """If the porcelain read fails, refuse -- never assume the tree is clean."""
        import rlsbl.commands.release as release_pkg

        _write(mock_git_repo, STATE_REL)

        def _boom(cmd, args=None, **kwargs):
            raise subprocess.CalledProcessError(128, ["git"])

        monkeypatch.setattr(release_pkg, "run", _boom)
        with pytest.raises(ReleaseValidationError):
            validate_clean_tree({})


class TestGitignoreTemplateStateEntries:
    """Secondary half: the scaffold template ignores the state files."""

    EXPECTED = [
        ".rlsbl/releases/in-progress.json",
        ".rlsbl/releases/scrub-result.json",
        ".rlsbl-monorepo/releasables/*/releases/in-progress.json",
        ".rlsbl-monorepo/releasables/*/releases/scrub-result.json",
    ]

    def _template_text(self):
        from importlib.resources import files as pkg_files

        return (
            pkg_files("rlsbl") / "templates" / "shared" / "gitignore.tpl"
        ).read_text()

    def test_template_carries_the_state_entries(self):
        lines = {line.strip() for line in self._template_text().splitlines()}
        for entry in self.EXPECTED:
            assert entry in lines, f"gitignore.tpl is missing {entry}"

    def test_template_does_not_ignore_the_committed_plan_sidecar(self):
        assert "unreleased.plan.json" not in self._template_text()

    def test_rescaffold_merges_the_entries_additively(self, tmp_path, monkeypatch):
        """Re-scaffold appends the new lines and keeps local customizations."""
        import rlsbl
        from rlsbl.commands.init_cmd import plan_mappings

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gitignore").write_text(
            "node_modules/\n__pycache__/\nmy-local-thing/\n"
        )

        template_dir = os.path.join(
            os.path.dirname(rlsbl.__file__), "templates", "shared",
        )
        plans = plan_mappings(
            template_dir,
            [{"template": "gitignore.tpl", "target": ".gitignore"}],
            {},
        )
        plan = plans[0]
        assert plan["status"] == "updated (additive merge)"
        merged_lines = {line.strip() for line in plan["content"].splitlines()}
        for entry in self.EXPECTED:
            assert entry in merged_lines
        assert "my-local-thing/" in merged_lines

    def test_rescaffold_is_idempotent_once_entries_are_present(self, tmp_path, monkeypatch):
        import rlsbl
        from rlsbl.commands.init_cmd import plan_mappings

        monkeypatch.chdir(tmp_path)
        template_dir = os.path.join(
            os.path.dirname(rlsbl.__file__), "templates", "shared",
        )
        with open(os.path.join(template_dir, "gitignore.tpl"), encoding="utf-8") as f:
            (tmp_path / ".gitignore").write_text(f.read())

        plans = plan_mappings(
            template_dir,
            [{"template": "gitignore.tpl", "target": ".gitignore"}],
            {},
        )
        assert plans[0]["status"] == "unchanged"
