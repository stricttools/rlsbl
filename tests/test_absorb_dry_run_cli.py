"""`rlsbl monorepo absorb --dry-run`, driven through the real CLI.

Every other absorb dry-run test calls ``cmd_absorb(dry_run=True)`` directly.
That bypasses the strictcli dispatch, so no effects handle is ever minted and
the preview machinery is not in the loop -- which is exactly why the suite
never saw the defect this module pins:

``validate_absorb_preconditions`` runs BEFORE ``cmd_absorb``'s dry-run early
return, and it read the source worktree with a ``git status --porcelain`` that
carried no ``--no-optional-locks``.  The allowlisted status form (see
``rlsbl/observe_allowlist.py``) is ``git --no-optional-locks status``, so under
a real preview that argv was RECORDED instead of run, ``.stdout`` off the
recorded carrier raised strictcli's truncation error, and
``rlsbl monorepo absorb --dry-run`` died before it could preview anything.

The test therefore has to go through ``rlsbl.app.test([...])``, so the effects
handle, the recording and the allowlist the real app was built with are all the
real ones.  The only stand-in below the dispatch is the git-filter-repo
PRESENCE probe (see the fixture); the seam under test is untouched.
"""

import os
import subprocess

import pytest

import rlsbl
from rlsbl.commands.monorepo import extract as extract_mod


@pytest.fixture(autouse=True)
def _filter_repo_present(monkeypatch):
    """Report ``git-filter-repo`` as installed, unconditionally.

    ``validate_absorb_preconditions`` opens with a PRESENCE probe for
    git-filter-repo; the dry-run path never invokes the tool itself, since it
    returns before the history rewrite.  The probe is stubbed rather than
    skipped-around so these tests behave identically bare and inside the
    sandbox runner (which does not put ``~/.local/bin`` on PATH, so a
    ``shutil.which``-driven skipif would silently take the regression out of
    CI).  Nothing else below the dispatch is replaced.

    The limit of that stand-in, stated so nobody reads more coverage into
    these tests than they carry: because the probe is answered "present"
    unconditionally, a filter-repo call ADDED to the dry-run path later would
    not be exercised here for the missing-binary case -- the preview would
    walk straight past the refusal that a real absent binary produces.
    """
    monkeypatch.setattr(
        extract_mod, "require_filter_repo", lambda: "/stub/git-filter-repo"
    )


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@test.local")
    _git(path, "config", "user.name", "Test")
    return path


def _commit(repo, rel, content, message):
    fp = repo / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _make_source_repo(tmp_path):
    """A clean external repo with one version tag, ready to be absorbed."""
    src = _init_repo(tmp_path / "widget_src")
    _commit(src, "package.json", '{"name": "widget", "version": "0.1.0"}\n',
            "feat: initial widget")
    _git(src, "tag", "v0.1.0")
    return src


def _make_monorepo(tmp_path):
    """A committed monorepo workspace with one unrelated member."""
    root = _init_repo(tmp_path / "mono")
    ws = root / ".rlsbl-monorepo"
    ws.mkdir()
    (ws / "workspace.toml").write_text(
        '[[projects]]\npath = "existing"\nname = "existing"\n'
    )
    _commit(root, "existing/keep.txt", "keep\n", "add existing")
    _git(root, "add", ".rlsbl-monorepo/workspace.toml")
    _git(root, "commit", "-q", "-m", "workspace")
    return root


class TestAbsorbDryRunThroughTheCli:
    def test_preview_survives_the_precondition_read(self, tmp_path, monkeypatch):
        """`monorepo absorb --dry-run` previews instead of dying on an observe.

        The precondition check reads the source worktree before the dry-run
        early return. If that read is not on the observe allowlist, the
        preview records it, the caller reads ``.stdout`` off a carrier, and
        the handler's ``except Exception`` turns the truncation into
        ``Error: ...`` plus exit 1 -- no preview at all.
        """
        root = _make_monorepo(tmp_path)
        source = _make_source_repo(tmp_path)
        head_before = _git(root, "rev-parse", "HEAD")

        monkeypatch.chdir(root)
        result = rlsbl.app.test(
            ["--dry-run", "monorepo", "absorb", str(source), "packages/widget"]
        )

        assert result.exit_code == 0, (
            "the preview failed before it could report anything; "
            f"stderr was:\n{result.stderr}"
        )
        assert "Would absorb 'widget'" in result.stdout, result.stdout
        assert "0.1.0" in result.stdout, (
            "the preview must name the version tags it would import; "
            f"stdout was:\n{result.stdout}"
        )

        # And it really was a preview: the monorepo is untouched.
        assert _git(root, "rev-parse", "HEAD") == head_before
        assert not os.path.exists(os.path.join(str(root), "packages", "widget"))

    def test_preview_reports_a_dirty_source_instead_of_a_truncation(
        self, tmp_path, monkeypatch
    ):
        """The precondition still DECIDES under a preview.

        The status read is on the allowlist because it must really run: a
        recorded read would hand back a carrier, and the dirty-source refusal
        the check exists for would never fire.
        """
        root = _make_monorepo(tmp_path)
        source = _make_source_repo(tmp_path)
        (source / "uncommitted.txt").write_text("dirty\n")

        monkeypatch.chdir(root)
        result = rlsbl.app.test(
            ["--dry-run", "monorepo", "absorb", str(source), "packages/widget"]
        )

        assert result.exit_code == 1
        assert "uncommitted changes" in result.stderr, result.stderr
