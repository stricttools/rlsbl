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

import json
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


def _track_releases_dir(repo):
    """Commit the release file, so git reports the state file by name.

    ``git status`` collapses a WHOLLY untracked directory into one
    ``?? .rlsbl/releases/`` record, which names no file to classify. Every real
    rlsbl project has committed ``unreleased.toml`` (and its archives) in that
    directory long before a release writes state into it, so this is the shape
    the checks actually meet.
    """
    rel = os.path.join(".rlsbl", "releases", "unreleased.toml")
    _write(repo, rel, "")
    subprocess.run(["git", "add", rel], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "release file"], cwd=str(repo), check=True,
    )


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

    def test_a_dirty_unicode_path_is_named_as_it_is_on_disk(self, mock_git_repo):
        """The refusal must name the file, not git's C-escaped spelling of it.

        ``git status --porcelain`` quotes any path outside plain ASCII, so a
        parser that reads the default output reports
        ``"docs/na\\303\\257ve note.md"`` -- a name no filesystem has, which the
        operator cannot copy, paste or find.
        """
        _write(mock_git_repo, os.path.join("docs", "naïve note.md"), "wip\n")

        with pytest.raises(ReleaseValidationError) as exc:
            validate_clean_tree({})

        message = str(exc.value)
        assert "docs/naïve note.md" in message
        assert "\\303" not in message
        assert '"' not in message

    def test_allow_dirty_baseline_carries_the_real_unicode_path(self, mock_git_repo):
        """The baseline set feeds the unexpected-files guard and the commit.

        An escaped spelling in it makes the guard compare two different names
        for the same file and flag the release's own work as foreign.
        """
        (mock_git_repo / "naïve note.md").write_text("wip\n")

        dirty = validate_clean_tree({"allow-dirty": True})

        assert dirty == {"naïve note.md"}

    def test_unreadable_status_fails_closed(self, mock_git_repo, monkeypatch):
        """If the porcelain read fails, refuse -- never assume the tree is clean.

        The read is the shared ``working_tree_paths`` helper, so that is what
        this pins: whatever it raises must come out as a refusal.
        """
        import rlsbl.commands.release.validate as validate_mod

        _write(mock_git_repo, STATE_REL)

        def _boom(*args, **kwargs):
            raise subprocess.CalledProcessError(128, ["git"])

        monkeypatch.setattr(validate_mod, "working_tree_paths", _boom)
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

    def test_template_does_not_ignore_the_committed_plan_file(self):
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


# ---------------------------------------------------------------------------
# The shared subtraction, and the other commands that perform it
# ---------------------------------------------------------------------------

class TestBlockingDirtyPaths:
    """`blocking_dirty_paths` is the one place the subtraction is spelled."""

    def test_tool_state_alone_yields_nothing(self, mock_git_repo):
        from rlsbl.commands.release.validate import blocking_dirty_paths

        _write(mock_git_repo, STATE_REL)
        _write(mock_git_repo, SCRUB_REL)
        assert blocking_dirty_paths() == []

    def test_other_dirt_is_reported(self, mock_git_repo):
        from rlsbl.commands.release.validate import blocking_dirty_paths

        _write(mock_git_repo, STATE_REL)
        _write(mock_git_repo, "notes.txt", "wip\n")
        assert blocking_dirty_paths() == ["notes.txt"]

    def test_cwd_selects_the_repository(self, mock_git_repo):
        """The helper answers about the repo it is pointed at, not the process cwd."""
        from rlsbl.commands.release.validate import blocking_dirty_paths

        _write(mock_git_repo, "notes.txt", "wip\n")
        assert blocking_dirty_paths(cwd=str(mock_git_repo)) == ["notes.txt"]


class TestOtherCleanTreeChecksExemptToolState:
    """Every clean-tree refusal reuses the predicate, never a second list.

    Each of these commands refuses over a dirty tree and, without the
    exemption, refused over `.rlsbl/releases/in-progress.json` -- a file rlsbl
    itself wrote, in a repository whose `.gitignore` never got an entry for it.
    For the rename and the conversions, the refusal also preempted the specific
    in-flight-release refusal that follows it and actually explains the state.
    """

    def test_releasable_rename_exempts_tool_state(self, mock_git_repo):
        from rlsbl.commands.monorepo.releasable_rename import _blocking_dirty_paths

        _write(mock_git_repo, STATE_REL)
        assert _blocking_dirty_paths(str(mock_git_repo)) == []

        _write(mock_git_repo, "notes.txt", "wip\n")
        assert _blocking_dirty_paths(str(mock_git_repo)) == ["notes.txt"]

    def test_extract_exempts_tool_state(self, mock_git_repo):
        from rlsbl.commands.monorepo.extract_cmd import _dirty_paths

        _track_releases_dir(mock_git_repo)
        _write(mock_git_repo, STATE_REL)
        assert _dirty_paths(str(mock_git_repo)) == []

        _write(mock_git_repo, "notes.txt", "wip\n")
        assert _dirty_paths(str(mock_git_repo)) == ["notes.txt"]

    def test_absorb_exempts_tool_state(self, mock_git_repo):
        from rlsbl.commands.monorepo.absorb_cmd import _dirty_paths

        _track_releases_dir(mock_git_repo)
        _write(mock_git_repo, STATE_REL)
        assert _dirty_paths(str(mock_git_repo)) == []

        _write(mock_git_repo, "notes.txt", "wip\n")
        assert _dirty_paths(str(mock_git_repo)) == ["notes.txt"]


class TestUndoCleanTreeExemption:
    """`rlsbl release undo` refused over rlsbl's own untracked state file.

    Undo's clean-tree refusal read every dirty path git reported, with no
    exemption at all -- while the release path exempted the two tool-owned
    state files. `.rlsbl/releases/in-progress.json` is written untracked, and a
    consumer repository whose `.gitignore` predates the scaffold entry for it
    therefore reports it as dirty. Undo refused, printing "commit your changes
    first" -- a remedy nobody should follow for a file rlsbl wrote and deletes
    itself, and the refusal fired in the very situation undo exists for.
    """

    def _repo(self, tmp_path, monkeypatch):
        """A released 1.0.1 repository whose `.gitignore` covers nothing."""
        from test_undo import _make_real_shape_repo

        repo = tmp_path / "repo"
        _make_real_shape_repo(repo)
        (repo / ".gitignore").write_text("")
        subprocess.run(["git", "add", ".gitignore"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "an ignore file that ignores nothing"],
            cwd=str(repo), check=True,
        )
        monkeypatch.chdir(repo)
        return repo

    def _state_for(self, repo, version):
        _write(repo, STATE_REL, json.dumps({
            "new_version": version,
            "tag": f"v{version}",
            "branch": "main",
            "completed_steps": [],
        }) + "\n")

    def test_untracked_scrub_state_does_not_block_undo(
        self, tmp_path, monkeypatch, capsys,
    ):
        from test_undo import _run_undo

        repo = self._repo(tmp_path, monkeypatch)
        _write(repo, SCRUB_REL)

        _run_undo(repo, {"dry-run": True})

        assert "v1.0.1" in capsys.readouterr().out

    def test_untracked_release_state_does_not_block_undo(
        self, tmp_path, monkeypatch, capsys,
    ):
        """The live case: the state file of the release being undone."""
        from test_undo import _run_undo

        repo = self._repo(tmp_path, monkeypatch)
        self._state_for(repo, "1.0.1")

        _run_undo(repo, {"dry-run": True})

        assert "v1.0.1" in capsys.readouterr().out

    def test_any_other_dirty_file_still_refuses_and_names_it(
        self, tmp_path, monkeypatch, capsys,
    ):
        from test_undo import _run_undo

        repo = self._repo(tmp_path, monkeypatch)
        _write(repo, SCRUB_REL)
        _write(repo, "notes.txt", "wip\n")

        with pytest.raises(SystemExit) as exc:
            _run_undo(repo, {"dry-run": True})

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "notes.txt" in err
        assert "scrub-result.json" not in err

    def test_following_the_commit_remedy_clears_the_refusal(
        self, tmp_path, monkeypatch, capsys,
    ):
        """"Commit your changes first" is executed, and the refusal is gone.

        The remedy is only honest for the paths the refusal names: committing
        them must actually let undo through, with the tool-owned state file
        still sitting there untracked.
        """
        from test_undo import _run_undo

        repo = self._repo(tmp_path, monkeypatch)
        self._state_for(repo, "1.0.1")
        _write(repo, "notes.txt", "wip\n")

        with pytest.raises(SystemExit):
            _run_undo(repo, {"dry-run": True})
        assert "notes.txt" in capsys.readouterr().err

        subprocess.run(["git", "add", "notes.txt"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "notes"], cwd=str(repo), check=True,
        )

        _run_undo(repo, {"dry-run": True})

        assert "v1.0.1" in capsys.readouterr().out
        assert (repo / STATE_REL).exists(), "the state file was left alone"
