"""Regression test for Phase 5.2: changelog-orphans must thread the per-context
tag_glob.

check_changelog_orphans previously called check_no_orphans(entries) discarding
the tag_glob/project from the context tuple, so a releasable with a non-default
tag range fell back to the default "v*" glob. An entry whose commit is in the
releasable's range but not the default range was then falsely flagged as an
orphan. Threading the tag_glob (as the sibling range/coverage checks do) fixes
it.
"""

import json
import os
from pathlib import Path

from conftest import git_head, run_git
from test_coverage_check_releasable import (
    _make_workspace_ctx,
    _setup_releasable_monorepo,
)

from rlsbl import app
from rlsbl.workspace import Releasable, get_releasable_changes_dir


class TestChangelogOrphansThreadsTagGlob:
    def test_in_range_entry_not_orphaned_with_stray_default_tag(self, tmp_path, monkeypatch):
        """A commit in the releasable's range must not be flagged as an orphan
        merely because a stray default-glob (``v*``) tag exists after it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        releasables = [Releasable(name="alpha")]  # tag_format: alpha@v{version}
        projects = [
            {"path": "libs/core", "name": "core", "releasable": "alpha"},
        ]
        _setup_releasable_monorepo(repo, releasables=releasables, projects=projects)
        # -> tag alpha@v0.1.0 now exists at scaffold commit.

        # Commit that belongs to the alpha releasable range (after alpha@v0.1.0).
        core_dir = repo / "libs" / "core"
        (core_dir / "feat.py").write_text("y = 2\n")
        run_git(repo, "add", "libs/core/feat.py")
        run_git(repo, "commit", "-q", "-m", "feat: covered change")
        covered_sha = git_head(repo)

        # A stray default-glob tag ("v*") AFTER the covered commit. With the
        # default glob, the unreleased range would be v9.9.9..HEAD, which
        # EXCLUDES covered_sha -- the exact condition that produced the false
        # orphan before the fix.
        (repo / "unrelated.txt").write_text("x\n")
        run_git(repo, "add", "unrelated.txt")
        run_git(repo, "commit", "-q", "-m", "unrelated")
        run_git(repo, "tag", "v9.9.9")

        # Cover the commit in the alpha releasable's JSONL.
        changes_dir = get_releasable_changes_dir(str(repo), "alpha")
        entry = json.dumps({
            "commits": [covered_sha],
            "user_facing": True,
            "description": "covered feature",
            "type": "feature",
        })
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write(entry + "\n")
        run_git(repo, "add", os.path.join(changes_dir, "unreleased.jsonl"))
        run_git(repo, "commit", "-q", "-m", "add entry")

        ctx = _make_workspace_ctx(repo, releasables)
        result = app._check_defs["changelog-orphans"].impl(ctx)
        assert result.status == "pass", (
            f"Expected pass, got {result.status}: {result.message} {result.details}"
        )
