"""Integration tests for ``rlsbl monorepo rename-releasable``.

These build a real-git monorepo with a bare remote, per-member CI/publish
workflows, and releasable tags, then exercise the full rename flow: gate
prefix flip in the regenerated publish.yml, idempotent re-run, crash healing
between commit and tag push, the no-``{name}`` tag_format shortcut, changelog
tag-glob resolution post-rename, the unmanaged-history note, and dry-run.
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from conftest import with_root_member

from githarness import git as _git
from rlsbl.workspace import (
    Releasable,
    load_releasables,
    load_workspace,
    read_releasable_version,
    save_workspace,
    get_releasable_changes_dir,
    get_releasable_dir,
    write_releasable_version,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
)
from rlsbl.tag_glob import resolve_monorepo_tag_glob
from rlsbl.commands.monorepo import releasable_rename as rr


CI_WF = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo test
"""

PUBLISH_WF = """\
name: Publish
on:
  workflow_dispatch:
    inputs:
      tag:
        description: tag
        required: true
permissions:
  contents: write
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo publish
"""


def _write_member(root, path, name, releasable, version="0.1.0"):
    d = root / path
    (d / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (d / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    (d / ".rlsbl").mkdir(exist_ok=True)
    (d / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n"
    )
    (d / ".github" / "workflows" / "ci.yml").write_text(CI_WF)
    (d / ".github" / "workflows" / "publish.yml").write_text(PUBLISH_WF)


def _build_monorepo(root, *, tag_format="{name}@v{version}", version="0.1.0",
                    add_post_tag_commit=False, create_tag=True):
    """Build a real-git monorepo with one releasable ('beta', 2 members)."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# mono\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")

    releasables = [Releasable(name="beta", tag_format=tag_format)]
    projects = [
        {"path": "libs/beta-api", "name": "beta-api", "releasable": "beta"},
        {"path": "apps/beta-cli", "name": "beta-cli", "releasable": "beta"},
    ]
    save_workspace(str(root), with_root_member(projects), releasables=releasables)

    for p in projects:
        _write_member(root, p["path"], p["name"], "beta", version)

    # Releasable state dir.
    write_releasable_version(str(root), "beta", version)
    changes = get_releasable_changes_dir(str(root), "beta")
    os.makedirs(changes, exist_ok=True)
    (root / ".rlsbl-monorepo" / "releasables" / "beta" / "changes"
     / "unreleased.jsonl").write_text("")
    (root / ".rlsbl-monorepo" / "releasables" / "beta"
     / "config.json").write_text("{}\n")

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add monorepo")

    # Generate root routers so publish.yml exists with the OLD prefix.
    from rlsbl.commands.monorepo import _cmd_sync
    _cmd_sync({"auto-commit": True}, project_root=str(root))

    # Tag the releasable at the current version (only for {name} formats we
    # care about a scoped tag; for plain formats this is still valid).
    if create_tag:
        the_tag = tag_format.format(name="beta", version=version)
        _git(root, "tag", the_tag)

    if add_post_tag_commit:
        (root / "libs" / "beta-api" / "extra.txt").write_text("more\n")
        _git(root, "add", "libs/beta-api/extra.txt")
        _git(root, "commit", "-q", "-m", "post-tag work")

    # Bare remote, push main + tags.
    remote = root.parent / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare"], cwd=str(remote), check=True)
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-q", "origin", "main", "--tags")
    return remote


def _read_ws(root):
    return (root / WORKSPACE_DIR / WORKSPACE_FILE).read_text()


def _publish_yml(root):
    return (root / ".github" / "workflows" / "publish.yml").read_text()


@pytest.fixture
def _gh_ok():
    with patch.object(rr, "check_gh_installed", return_value=True), \
         patch.object(rr, "check_gh_auth", return_value=True):
        yield


class TestFullRename:
    def test_flips_gate_prefix_and_pushes_alias_tag(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        remote = _build_monorepo(root)

        # Sanity: the OLD prefix is in the generated publish.yml.
        assert "beta@v" in _publish_yml(root)

        result = rr.rename_releasable(str(root), "beta", "beta2")

        # workspace.toml renamed (releasable + members), comments/format intact.
        ws = _read_ws(root)
        assert 'name = "beta2"' in ws
        assert 'releasable = "beta2"' in ws
        assert 'releasable = "beta"' not in ws

        # State dir moved.
        assert (root / ".rlsbl-monorepo" / "releasables" / "beta2").is_dir()
        assert not (root / ".rlsbl-monorepo" / "releasables" / "beta").exists()

        # Gate prefix flipped in regenerated publish.yml.
        pub = _publish_yml(root)
        assert "beta2@v" in pub
        assert "'beta@v'" not in pub

        # Alias tag created locally and pushed.
        assert rr._tag_exists_local(str(root), "beta2@v0.1.0")
        assert rr._tag_exists_remote(str(root), "origin", "beta2@v0.1.0")
        assert result["tag"]["status"] == "created"

        # The unmanaged-history note is emitted.
        assert "no longer managed" in result["note"]

        # Working tree is clean after the operation.
        assert rr._is_clean_tree(str(root))

    def test_second_run_noops(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        rr.rename_releasable(str(root), "beta", "beta2")
        # Second run: resume path detects everything already done.
        result = rr.rename_releasable(str(root), "beta", "beta2")
        assert result["mode"] == "resume"
        assert result["tag"]["status"] == "already_done"


class TestCrashHealing:
    def test_crash_between_commit_and_tag_push_is_healed(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        # Simulate a crash: do the local mutations + commit, but NOT the tag.
        rr._apply_local_rename(str(root), "beta", "beta2")
        assert not rr._tag_exists_local(str(root), "beta2@v0.1.0")
        assert rr._is_clean_tree(str(root))

        # Re-run the full command -> resume path finishes the tag step.
        result = rr.rename_releasable(str(root), "beta", "beta2")
        assert result["mode"] == "resume"
        assert rr._tag_exists_local(str(root), "beta2@v0.1.0")
        assert rr._tag_exists_remote(str(root), "origin", "beta2@v0.1.0")


class TestCrashBeforeCommitHealing:
    def test_crash_before_commit_is_healed(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        # Simulate a crash BEFORE the commit: apply the workspace.toml edit and
        # the directory move (as _apply_local_rename would) but stop short of the
        # sync + commit. The tree is now dirty and the gate prefix is stale.
        rr._apply_workspace_rename(str(root), "beta", "beta2")
        old_dir = get_releasable_dir(str(root), "beta")
        new_dir = get_releasable_dir(str(root), "beta2")
        os.rename(old_dir, new_dir)

        assert not rr._is_clean_tree(str(root)), "precondition: uncommitted rename"
        assert not rr._tag_exists_local(str(root), "beta2@v0.1.0")
        assert "beta@v" in _publish_yml(root), "precondition: gate prefix still stale"

        # Re-run the full command. A correct resume must HEAL completely:
        # commit the pending rename, regenerate the gate prefix, THEN push the
        # alias tag -- never push a tag over an uncommitted rename with a stale
        # publish gate.
        result = rr.rename_releasable(str(root), "beta", "beta2")

        # The rename is now committed: clean tree.
        assert rr._is_clean_tree(str(root)), \
            "re-run must commit the pending rename, not leave a dirty tree"
        # The gate prefix was regenerated and committed.
        assert "beta2@v" in _publish_yml(root)
        assert "'beta@v'" not in _publish_yml(root)
        # The alias tag was created and pushed only after the commit.
        assert rr._tag_exists_local(str(root), "beta2@v0.1.0")
        assert rr._tag_exists_remote(str(root), "origin", "beta2@v0.1.0")
        assert result["mode"] == "resume"


class TestNoNameTagFormat:
    def test_name_only_rename_skips_alias_and_gate(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root, tag_format="v{version}")

        before_tags = set(_git(root, "tag", "--list").splitlines())
        result = rr.rename_releasable(str(root), "beta", "beta2")

        # No alias tag created; name-only path taken.
        assert result.get("name_only") is True
        assert result["tag"] is None
        after_tags = set(_git(root, "tag", "--list").splitlines())
        assert before_tags == after_tags

        # Workspace + dir still renamed.
        assert 'name = "beta2"' in _read_ws(root)
        assert (root / ".rlsbl-monorepo" / "releasables" / "beta2").is_dir()


class TestChangelogGlobResolves:
    def test_new_glob_resolves_member_range(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root, add_post_tag_commit=True)

        rr.rename_releasable(str(root), "beta", "beta2")

        projects = load_workspace(str(root))
        releasables = load_releasables(str(root), projects)
        beta2 = next(r for r in releasables if r.name == "beta2")
        member = next(p for p in projects if p.name == "beta-api")

        glob = resolve_monorepo_tag_glob(member, str(root), releasable=beta2)
        assert glob == "beta2@v*"

        # git describe with the new glob resolves to the alias tag.
        described = _git(
            root, "describe", "--tags", "--abbrev=0", "--match", glob
        )
        assert described == "beta2@v0.1.0"

        # And a rev-list range against it is well-formed (non-empty: the
        # post-tag commit is in range).
        rng = _git(root, "rev-list", "--count", "beta2@v0.1.0..HEAD")
        assert int(rng) >= 1


class TestDryRun:
    def test_dry_run_zero_changes_lists_push(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        ws_before = _read_ws(root)
        pub_before = _publish_yml(root)
        tags_before = set(_git(root, "tag", "--list").splitlines())

        result = rr.rename_releasable(str(root), "beta", "beta2", dry_run=True)

        # Zero mutations.
        assert _read_ws(root) == ws_before
        assert _publish_yml(root) == pub_before
        assert set(_git(root, "tag", "--list").splitlines()) == tags_before
        assert (root / ".rlsbl-monorepo" / "releasables" / "beta").is_dir()
        assert not (root / ".rlsbl-monorepo" / "releasables" / "beta2").exists()

        # Plan lists the tag push explicitly.
        assert result["planned_push"] == "git push origin beta2@v0.1.0"
        assert any("git push origin beta2@v0.1.0" in line for line in result["plan"])


class TestNameOnlyRenameNoGhAuth:
    def test_name_only_rename_does_not_require_gh(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        # tag_format has no {name} -> no alias tag, no push -> gh auth not needed.
        _build_monorepo(root, tag_format="v{version}")

        # gh is explicitly unavailable; the name-only rename must still succeed.
        with patch.object(rr, "check_gh_installed", return_value=False), \
             patch.object(rr, "check_gh_auth", return_value=False):
            result = rr.rename_releasable(str(root), "beta", "beta2")

        assert result.get("name_only") is True
        assert result["tag"] is None
        assert 'name = "beta2"' in _read_ws(root)
        assert (root / ".rlsbl-monorepo" / "releasables" / "beta2").is_dir()


class TestDevNodeMembersUntouched:
    def test_releasable_false_project_is_not_renamed(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        # Add a dev-node project (releasable = false) with full member files so
        # sync can inline it, then commit.
        _write_member(root, "tools/devnode", "devnode", "beta")
        import tomlkit
        ws_path = root / WORKSPACE_DIR / WORKSPACE_FILE
        doc = tomlkit.loads(ws_path.read_text())
        proj = tomlkit.table()
        proj["path"] = "tools/devnode"
        proj["name"] = "devnode"
        proj["releasable"] = False
        doc["projects"].append(proj)
        ws_path.write_text(tomlkit.dumps(doc))
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "add devnode")

        rr.rename_releasable(str(root), "beta", "beta2")

        ws = _read_ws(root)
        # beta members renamed, but the dev-node project's releasable stays false.
        assert 'releasable = "beta2"' in ws
        assert 'releasable = "beta"' not in ws
        assert "releasable = false" in ws
        devnode = next(p for p in load_workspace(str(root)) if p.name == "devnode")
        assert devnode.releasable is False


class TestNeverReleasedNoSourceTag:
    def test_rename_of_never_released_releasable(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        # No current-version tag exists (releasable was never released).
        _build_monorepo(root, create_tag=False)
        assert not rr._tag_exists_local(str(root), "beta@v0.1.0")

        result = rr.rename_releasable(str(root), "beta", "beta2")

        # The tag step reports there was nothing to alias.
        assert result["tag"]["status"] == "no_source_tag"
        assert result["tag"]["old_tag"] == "beta@v0.1.0"
        # No alias tag was fabricated.
        assert not rr._tag_exists_local(str(root), "beta2@v0.1.0")
        # The local rename still completed and committed cleanly.
        assert 'name = "beta2"' in _read_ws(root)
        assert rr._is_clean_tree(str(root))


class TestAliasIsRecordedInLineage:
    """The alias tag a rename creates is a lineage FACT, not only a git ref.

    ``expected_refs`` reads recorded aliases from the lineage record, so a
    rename that creates an alias tag without recording it would leave the ref
    set with a second, undiscoverable source. One source: the lineage record.
    """

    def _aliases(self, root, name):
        from rlsbl.lineage import KIND_BOUNDARY_ALIAS, get_lineage_path, read_events
        from rlsbl.workspace import get_releasable_dir

        path = get_lineage_path(
            str(root), releasable_dir=get_releasable_dir(str(root), name),
        )
        if not os.path.isfile(path):
            return []
        out = []
        for event in read_events(path, kinds=[KIND_BOUNDARY_ALIAS]):
            out.extend(event.aliases)
        return out

    def test_the_created_alias_is_recorded(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        rr.rename_releasable(str(root), "beta", "beta2")

        aliases = self._aliases(root, "beta2")
        assert len(aliases) == 1
        alias = aliases[0]
        assert alias.alias_tag == "beta2@v0.1.0"
        assert alias.aliased_tag == "beta@v0.1.0"
        assert alias.commit == _git(root, "rev-list", "-n", "1", "beta2@v0.1.0")
        # Recorded means committed: the record is repository state.
        assert rr._is_clean_tree(str(root))

    def test_re_running_appends_no_duplicate(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        rr.rename_releasable(str(root), "beta", "beta2")
        rr.rename_releasable(str(root), "beta", "beta2")

        assert len(self._aliases(root, "beta2")) == 1

    def test_no_source_tag_records_nothing(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root, create_tag=False)

        rr.rename_releasable(str(root), "beta", "beta2")

        assert self._aliases(root, "beta2") == []

    def test_name_only_rename_records_nothing(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root, tag_format="v{version}")

        rr.rename_releasable(str(root), "beta", "beta2")

        assert self._aliases(root, "beta2") == []


class TestPreflight:
    """Preflight hard-errors use the multi_releasable fixture (git repo)."""

    def test_invalid_new_name_charset(self, multi_releasable_monorepo, _gh_ok):
        ns = multi_releasable_monorepo
        with pytest.raises(Exception) as ei:
            rr.rename_releasable(str(ns.root), "alpha", "Alpha_Bad")
        assert "invalid releasable name" in str(ei.value)

    def test_old_not_found(self, multi_releasable_monorepo, _gh_ok):
        ns = multi_releasable_monorepo
        with pytest.raises(Exception) as ei:
            rr.rename_releasable(str(ns.root), "nope", "gamma")
        assert "not found" in str(ei.value)

    def test_new_already_exists(self, multi_releasable_monorepo, _gh_ok):
        ns = multi_releasable_monorepo
        with pytest.raises(Exception) as ei:
            rr.rename_releasable(str(ns.root), "alpha", "beta")
        assert "already exists" in str(ei.value)

    def test_collision_with_project_name(self, multi_releasable_monorepo, _gh_ok):
        ns = multi_releasable_monorepo
        with pytest.raises(Exception) as ei:
            rr.rename_releasable(str(ns.root), "alpha", "beta-api")
        assert "collides" in str(ei.value)

    def test_dirty_tree_blocks(self, multi_releasable_monorepo, _gh_ok):
        ns = multi_releasable_monorepo
        (ns.root / "dirty.txt").write_text("uncommitted\n")
        with pytest.raises(Exception) as ei:
            rr.rename_releasable(str(ns.root), "alpha", "gamma")
        assert "not clean" in str(ei.value)


class TestNameValidator:
    def test_valid_names(self):
        for name in ("a", "abc", "a-b-c", "x1", "core2"):
            rr.validate_releasable_name(name)

    def test_invalid_names(self):
        for name in ("A", "1abc", "-abc", "a_b", "a.b", "", "a b"):
            with pytest.raises(Exception):
                rr.validate_releasable_name(name)


class TestAliasTagPushHygiene:
    """The single sanctioned remote write obeys the standard push contract."""

    def _capture_push(self, root, monkeypatch):
        """Run a rename with ``run`` wrapped so the tag push is recorded."""
        calls = []
        real_run = rr.run

        def spy(cmd, args, **kwargs):
            if cmd == "git" and args and args[0] == "push":
                calls.append((args, kwargs))
            return real_run(cmd, args, **kwargs)

        monkeypatch.setattr(rr, "run", spy)
        rr.rename_releasable(str(root), "beta", "beta2")
        assert calls, "no git push was issued"
        return calls[-1]

    def test_push_is_no_verify(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        args, _kwargs = self._capture_push(root, monkeypatch)
        assert "--no-verify" in args

    def test_push_uses_configured_timeout(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        rel_config = (root / ".rlsbl-monorepo" / "releasables" / "beta"
                      / "config.json")
        rel_config.write_text(json.dumps({"push_timeout": 321}) + "\n")
        _git(root, "add", str(rel_config.relative_to(root)))
        _git(root, "commit", "-q", "-m", "set push timeout")

        _args, kwargs = self._capture_push(root, monkeypatch)
        assert kwargs.get("timeout") == 321

    def test_push_defaults_to_the_standard_timeout(self, tmp_path, monkeypatch,
                                                   _gh_ok):
        from rlsbl.utils import DEFAULT_PUSH_TIMEOUT

        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        _args, kwargs = self._capture_push(root, monkeypatch)
        assert kwargs.get("timeout") == DEFAULT_PUSH_TIMEOUT
