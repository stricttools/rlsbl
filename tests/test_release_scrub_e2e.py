"""End-to-end tests for `rlsbl release scrub` against the REAL safegit binary.

The `safegit_bin` session fixture (conftest.py) builds the pinned safegit
release -- the exact version SAFEGIT_MIN_VERSION declares -- via
`go install`. These tests exercise real history rewrites in throwaway git
repos with a local bare remote. Only the GitHub boundary (gh) is mocked.

NO skip-if-absent: a missing Go toolchain fails these tests.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.release_scrub import run_cmd
from rlsbl.context import ProjectContext

MOD = "rlsbl.commands.release_scrub"

SECRET = "SECRETTOKEN123"
REPLACEMENT = "REDACTEDVALUE"


def _git(repo, *args, check=True):
    result = subprocess.run(
        ["git", *args], cwd=str(repo),
        capture_output=True, text=True, check=check,
    )
    return result.stdout.strip()


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "e2e@test.local")
    _git(repo, "config", "user.name", "E2E")


def _commit_file(repo, relpath, content, message):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _add_remote(repo, remote_dir):
    remote_dir.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "--bare"], cwd=str(remote_dir),
        capture_output=True, text=True, check=True,
    )
    _git(repo, "remote", "add", "origin", str(remote_dir))
    _git(repo, "push", "-q", "origin", "main", "--tags")


def _remote_ref(repo, refname):
    out = _git(repo, "ls-remote", "origin", refname)
    return out.split()[0] if out.split() else ""


def _jsonl_line(commits, user_facing=False, description=None, type_=None):
    entry = {"commits": commits, "user_facing": user_facing}
    if user_facing:
        entry["description"] = description or "A change"
        entry["type"] = type_ or "fix"
    return json.dumps(entry) + "\n"


@pytest.fixture
def e2e_env(safegit_bin, monkeypatch, tmp_path):
    """Prepend the pinned safegit to PATH and provide a work dir."""
    monkeypatch.setenv(
        "PATH", str(safegit_bin.parent) + os.pathsep + os.environ.get("PATH", "")
    )
    return tmp_path


# ===========================================================================
# Match mode: full monorepo flow (standalone + releasable changelog trees)
# ===========================================================================


class TestMatchModeFullFlowE2E:
    def test_full_monorepo_scrub(self, e2e_env, monkeypatch):
        ws = e2e_env / "ws"
        _init_repo(ws)

        # c1: the secret
        c1 = _commit_file(ws, "packages/alpha/config.env", f"token={SECRET}\n", "add config")
        # c2: unrelated
        c2 = _commit_file(ws, "packages/alpha/main.txt", "hello\n", "add main")

        # Workspace: alpha belongs to releasable "core"; beta is explicitly
        # unversioned but has its own (standalone-style) changelog tree.
        (ws / ".rlsbl-monorepo").mkdir()
        (ws / ".rlsbl-monorepo" / "workspace.toml").write_text(
            "[[projects]]\n"
            'path = "packages/alpha"\n'
            'name = "alpha"\n'
            'releasable = "core"\n'
            "[[projects]]\n"
            'path = "packages/beta"\n'
            'name = "beta"\n'
            "releasable = false\n"
            "[[releasables]]\n"
            'name = "core"\n'
        )
        _git(ws, "add", ".rlsbl-monorepo/workspace.toml")
        _git(ws, "commit", "-q", "-m", "workspace")

        # Standalone-style tree (beta) with full and ABBREVIATED hashes plus
        # a tracked .validated cache.
        beta_changes = ws / "packages" / "beta" / ".rlsbl" / "changes"
        beta_changes.mkdir(parents=True)
        (beta_changes / "unreleased.jsonl").write_text(_jsonl_line([c1]))
        (beta_changes / "1.0.0.jsonl").write_text(
            _jsonl_line([c2[:12]], user_facing=True, description="Short hash", type_="fix")
        )
        (beta_changes / ".validated").write_text(c2 + "\n")

        # Releasable-level tree (core) with a tracked .validated cache.
        core_changes = ws / ".rlsbl-monorepo" / "releasables" / "core" / "changes"
        core_changes.mkdir(parents=True)
        (core_changes / "unreleased.jsonl").write_text(_jsonl_line([c1]))
        (core_changes / ".validated").write_text(c2 + "\n")

        _git(ws, "add",
             "packages/beta/.rlsbl/changes/unreleased.jsonl",
             "packages/beta/.rlsbl/changes/1.0.0.jsonl",
             "packages/beta/.rlsbl/changes/.validated",
             ".rlsbl-monorepo/releasables/core/changes/unreleased.jsonl",
             ".rlsbl-monorepo/releasables/core/changes/.validated")
        _git(ws, "commit", "-q", "-m", "changelog trees")
        os.chmod(str(beta_changes / "1.0.0.jsonl"), 0o444)

        _git(ws, "tag", "-a", "v1.0.0", "-m", "release v1.0.0")
        _add_remote(ws, e2e_env / "remote")

        old_head = _git(ws, "rev-parse", "HEAD")
        old_remote_tag = _remote_ref(ws, "refs/tags/v1.0.0")

        monkeypatch.chdir(ws)
        alpha_dir = ws / "packages" / "alpha"
        ctx = ProjectContext(project_root=alpha_dir, workspace_root=ws, config={})
        flags = {
            "pattern": SECRET, "replace": REPLACEMENT,
            "reason": "remove leaked token", "entire-history": True, "yes": True,
        }
        with patch(f"{MOD}.check_gh_installed", return_value=False):
            run_cmd(flags, ctx=ctx)

        # --- Secret is gone from the entire object store ---
        objects = _git(ws, "rev-list", "--objects", "--all")
        for line in objects.splitlines():
            sha = line.split()[0]
            typ = _git(ws, "cat-file", "-t", sha)
            if typ == "blob":
                blob = _git(ws, "cat-file", "-p", sha, check=False)
                assert SECRET not in blob

        # --- Both changelog trees remapped to resolvable full SHAs ---
        for jsonl in [
            beta_changes / "unreleased.jsonl",
            beta_changes / "1.0.0.jsonl",
            core_changes / "unreleased.jsonl",
        ]:
            for line in jsonl.read_text().splitlines():
                for h in json.loads(line)["commits"]:
                    assert len(h) == 40, f"{jsonl}: {h} not a full SHA"
                    assert h not in (c1, c2), f"{jsonl}: old hash survived"
                    _git(ws, "rev-parse", "--verify", f"{h}^{{commit}}")

        # --- .validated deleted in BOTH trees and the deletion committed ---
        assert not (beta_changes / ".validated").exists()
        assert not (core_changes / ".validated").exists()
        assert _git(ws, "status", "--porcelain") == "", \
            "working tree must be clean after a scrub"

        # --- Scrub metadata commit with audit trailer ---
        last_msg = _git(ws, "log", "-1", "--format=%B")
        assert last_msg.startswith("scrub: remove leaked token")
        assert re.search(r"Scrub-remap: [0-9a-f]{40}\.\.[0-9a-f]{40}", last_msg)

        # --- Committed audit archive under the RELEASABLE dir, whitelisted ---
        scrubs_dir = ws / ".rlsbl-monorepo" / "releasables" / "core" / "scrubs"
        archives = list(scrubs_dir.glob("scrub-*.json"))
        assert len(archives) == 1
        archive_text = archives[0].read_text()
        assert SECRET not in archive_text
        assert REPLACEMENT not in archive_text
        archive = json.loads(archive_text)
        assert set(archive.keys()) == {
            "schema_version", "mode", "reason", "old_head", "new_head",
            "rewrites", "tags", "commits_rewritten", "completed_steps",
        }
        # Tracked (committed), not a stray file
        _git(ws, "ls-files", "--error-unmatch",
             str(archives[0].relative_to(ws)))

        # --- State cleared ---
        state = ws / ".rlsbl-monorepo" / "releasables" / "core" / "releases" / "scrub-result.json"
        assert not state.exists()

        # --- No stray CHANGELOG.md generated for alpha (no changes dir) ---
        assert not (alpha_dir / "CHANGELOG.md").exists()

        # --- Remote updated: branch (rewritten + metadata commit) and tag ---
        new_head = _git(ws, "rev-parse", "HEAD")
        assert new_head != old_head
        assert _remote_ref(ws, "refs/heads/main") == new_head
        new_remote_tag = _remote_ref(ws, "refs/tags/v1.0.0")
        assert new_remote_tag and new_remote_tag != old_remote_tag
        assert new_remote_tag == _git(ws, "rev-parse", "refs/tags/v1.0.0")


# ===========================================================================
# File mode (the red test proving bug 1: file mode works against real safegit)
# ===========================================================================


class TestFileModeE2E:
    def test_file_mode_scrub(self, e2e_env, monkeypatch):
        repo = e2e_env / "repo"
        _init_repo(repo)

        c1 = _commit_file(repo, "secrets.env", f"password={SECRET}\n", "add secrets")
        _commit_file(repo, "app.txt", "app\n", "add app")
        _git(repo, "rm", "-q", "secrets.env")
        _git(repo, "commit", "-q", "-m", "remove secrets")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text(_jsonl_line([c1]))
        (repo / "CHANGELOG.md").write_text("# Changelog\n")
        _git(repo, "add", ".rlsbl/changes/unreleased.jsonl", "CHANGELOG.md")
        _git(repo, "commit", "-q", "-m", "changelog")

        _add_remote(repo, e2e_env / "remote")

        monkeypatch.chdir(repo)
        ctx = ProjectContext(project_root=repo, workspace_root=None, config={})
        flags = {
            "file": "secrets.env",
            "from-commit": c1,
            "reason": "remove secrets file",
            "yes": True,
        }
        with patch(f"{MOD}.check_gh_installed", return_value=False):
            run_cmd(flags, ctx=ctx)

        # secrets.env is gone from every historical tree
        for sha in _git(repo, "rev-list", "--all").splitlines():
            files = _git(repo, "ls-tree", "-r", "--name-only", sha)
            assert "secrets.env" not in files.splitlines()

        # Changelog remapped to a resolvable hash; flow completed cleanly
        line = (changes / "unreleased.jsonl").read_text().splitlines()[0]
        new_hash = json.loads(line)["commits"][0]
        assert new_hash != c1
        _git(repo, "rev-parse", "--verify", f"{new_hash}^{{commit}}")
        assert _git(repo, "status", "--porcelain") == ""
        assert not (repo / ".rlsbl" / "releases" / "scrub-result.json").exists()
        assert _remote_ref(repo, "refs/heads/main") == _git(repo, "rev-parse", "HEAD")


# ===========================================================================
# Dry run (bug 2): preview shows non-zero counts, mutates nothing
# ===========================================================================


class TestDryRunE2E:
    def test_match_dry_run_shows_nonzero_counts(self, e2e_env, monkeypatch, capsys):
        repo = e2e_env / "repo"
        _init_repo(repo)
        _commit_file(repo, "config.env", f"token={SECRET}\n", "add config")
        _commit_file(repo, "other.txt", "x\n", "other")
        _add_remote(repo, e2e_env / "remote")
        old_head = _git(repo, "rev-parse", "HEAD")

        monkeypatch.chdir(repo)
        ctx = ProjectContext(project_root=repo, workspace_root=None, config={})
        flags = {
            "pattern": SECRET, "replace": REPLACEMENT,
            "reason": "preview", "entire-history": True, "dry-run": True,
        }
        with patch(f"{MOD}.check_gh_installed", return_value=False):
            run_cmd(flags, ctx=ctx)

        out = capsys.readouterr().out
        m = re.search(r"(\d+) matches", out)
        assert m and int(m.group(1)) > 0, f"expected non-zero matches in: {out!r}"
        m = re.search(r"~(\d+) commits would be rewritten", out)
        assert m and int(m.group(1)) > 0, f"expected non-zero commits in: {out!r}"

        # Nothing mutated
        assert _git(repo, "rev-parse", "HEAD") == old_head
        assert not (repo / ".rlsbl" / "releases").exists()


# ===========================================================================
# Recipe mode (bug 6)
# ===========================================================================


class TestRecipeModeE2E:
    def test_recipe_scrub(self, e2e_env, monkeypatch):
        repo = e2e_env / "repo"
        _init_repo(repo)
        c1 = _commit_file(
            repo, "config.env",
            f"token={SECRET}\napikey=OTHERSECRET456\n", "add config",
        )
        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text(_jsonl_line([c1]))
        _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        _git(repo, "commit", "-q", "-m", "changelog")
        _add_remote(repo, e2e_env / "remote")

        recipe = e2e_env / "recipe.toml"
        recipe.write_text(
            "[[operations]]\n"
            f'pattern = "{SECRET}"\n'
            f'replace = "{REPLACEMENT}"\n'
            "[[operations]]\n"
            'pattern = "OTHERSECRET456"\n'
            'replace = "REDACTED2"\n'
        )

        monkeypatch.chdir(repo)
        ctx = ProjectContext(project_root=repo, workspace_root=None, config={})
        flags = {
            "recipe": str(recipe),
            "reason": "multi-op cleanup", "entire-history": True, "yes": True,
        }
        with patch(f"{MOD}.check_gh_installed", return_value=False):
            run_cmd(flags, ctx=ctx)

        content = (repo / "config.env").read_text()
        assert SECRET not in content
        assert "OTHERSECRET456" not in content
        for sha in _git(repo, "rev-list", "--all").splitlines():
            show = _git(repo, "show", f"{sha}:config.env", check=False)
            assert SECRET not in show
            assert "OTHERSECRET456" not in show

        # Changelog remapped, flow completed
        line = (changes / "unreleased.jsonl").read_text().splitlines()[0]
        new_hash = json.loads(line)["commits"][0]
        assert new_hash != c1
        _git(repo, "rev-parse", "--verify", f"{new_hash}^{{commit}}")
        assert _git(repo, "status", "--porcelain") == ""


# ===========================================================================
# Corrupted/partial map: gate aborts before commit with resume intact (bug 5)
# ===========================================================================


class TestValidationGateAbortE2E:
    def test_dangling_hash_aborts_before_commit(self, e2e_env, monkeypatch, capsys):
        repo = e2e_env / "repo"
        _init_repo(repo)
        c1 = _commit_file(repo, "config.env", f"token={SECRET}\n", "add config")

        bogus = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text(
            _jsonl_line([c1]) + _jsonl_line([bogus])
        )
        _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        _git(repo, "commit", "-q", "-m", "changelog")
        _add_remote(repo, e2e_env / "remote")
        old_remote_head = _remote_ref(repo, "refs/heads/main")

        monkeypatch.chdir(repo)
        ctx = ProjectContext(project_root=repo, workspace_root=None, config={})
        flags = {
            "pattern": SECRET, "replace": REPLACEMENT,
            "reason": "cleanup", "entire-history": True, "yes": True,
        }
        with patch(f"{MOD}.check_gh_installed", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                run_cmd(flags, ctx=ctx)
        assert exc_info.value.code == 1

        err = capsys.readouterr().err
        assert bogus in err

        # Aborted BEFORE commit: no scrub metadata commit on the branch
        assert "scrub:" not in _git(repo, "log", "--format=%s")

        # Resume state intact; nothing pushed
        assert (repo / ".rlsbl" / "releases" / "scrub-result.json").exists()
        assert _remote_ref(repo, "refs/heads/main") == old_remote_head
