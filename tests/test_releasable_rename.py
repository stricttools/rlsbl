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
        _build_monorepo(root)

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
        assert "keep the tags they shipped under" in result["note"]

        # Working tree is clean after the operation.
        assert rr._blocking_dirty_paths(str(root)) == []

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


class TestTheRenameIsRecorded:
    """A rename leaves a `releasable-rename` event beside its boundary alias.

    Recorded in the REPOSITORY-scoped record: after the rename the releasable's
    own state directory exists only under the NEW name, while the old name is
    the spelling a reader holding a pre-rename tag will look up.
    """

    def _events(self, root):
        from rlsbl.transition_record import (
            KIND_RELEASABLE_RENAME,
            read_events,
            repository_transition_record_path,
        )

        return read_events(
            repository_transition_record_path(str(root)),
            kinds=[KIND_RELEASABLE_RENAME],
        )

    def test_the_event_names_both_spellings(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        rr.rename_releasable(str(root), "beta", "beta2")

        events = self._events(root)
        assert len(events) == 1, events
        assert (events[0].old_name, events[0].new_name) == ("beta", "beta2")
        assert events[0].reason
        # Committed with everything else the rename wrote.
        assert rr._blocking_dirty_paths(str(root)) == []

    def test_a_re_run_does_not_duplicate_it(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        rr.rename_releasable(str(root), "beta", "beta2")
        rr.rename_releasable(str(root), "beta", "beta2")

        assert len(self._events(root)) == 1

    def test_a_name_only_rename_is_recorded_too(self, tmp_path, monkeypatch, _gh_ok):
        """No alias tag is created, but the releasable was still renamed."""
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root, tag_format="v{version}")

        rr.rename_releasable(str(root), "beta", "beta2")

        events = self._events(root)
        assert [(e.old_name, e.new_name) for e in events] == [("beta", "beta2")]


class TestCrashHealing:
    def test_crash_between_commit_and_tag_push_is_healed(self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)

        # Simulate a crash: do the local mutations + commit, but NOT the tag.
        rr._apply_local_rename(str(root), "beta", "beta2")
        assert not rr._tag_exists_local(str(root), "beta2@v0.1.0")
        assert rr._blocking_dirty_paths(str(root)) == []

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

        assert rr._blocking_dirty_paths(str(root)), "precondition: uncommitted rename"
        assert not rr._tag_exists_local(str(root), "beta2@v0.1.0")
        assert "beta@v" in _publish_yml(root), "precondition: gate prefix still stale"

        # Re-run the full command. A correct resume must HEAL completely:
        # commit the pending rename, regenerate the gate prefix, THEN push the
        # alias tag -- never push a tag over an uncommitted rename with a stale
        # publish gate.
        result = rr.rename_releasable(str(root), "beta", "beta2")

        # The rename is now committed: clean tree.
        assert rr._blocking_dirty_paths(str(root)) == [], \
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
        assert rr._blocking_dirty_paths(str(root)) == []


class TestAliasIsRecordedInTransitionRecord:
    """The alias tag a rename creates is a transition record FACT, not only a git ref.

    ``expected_refs`` reads recorded aliases from the transition record, so a
    rename that creates an alias tag without recording it would leave the ref
    set with a second, undiscoverable source. One source: the transition record.
    """

    def _aliases(self, root, name):
        from rlsbl.transition_record import KIND_BOUNDARY_ALIAS, get_transition_record_path, read_events
        from rlsbl.workspace import get_releasable_dir

        path = get_transition_record_path(
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
        assert rr._blocking_dirty_paths(str(root)) == []

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


# ---------------------------------------------------------------------------
# Past releases keep the tag they shipped under
# ---------------------------------------------------------------------------


def _release(root, releasable, version, *, tag_format="{name}@v{version}"):
    """Release *version* of *releasable*: bump, tag, archive, push.

    The archive records the tagged commit as its release commit and every
    member path's tree at it, which is what the release flow writes.
    """
    from rlsbl.release_file import get_releases_dir, write_archived_release_file

    write_releasable_version(str(root), releasable, version)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", f"{releasable} v{version}")
    tag = tag_format.format(name=releasable, version=version)
    _git(root, "tag", tag)
    sha = _git(root, "rev-parse", "HEAD").strip()
    trees = {
        path: _git(root, "rev-parse", f"HEAD:{path}").strip()
        for path in ("libs/beta-api", "apps/beta-cli")
    }
    write_archived_release_file(
        get_releases_dir(str(root), releasable_dir=get_releasable_dir(str(root), releasable)),
        version, bump="minor", include=["pypi"],
        description=f"Version {version}.", candidate_sha=sha, tree_hashes=trees,
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"archive {tag}")
    _git(root, "push", "-q", "origin", "main", tag)
    return tag


def _preview(root, releasable, *, released_tags):
    """The reconcile preview for *releasable*, with GitHub Releases on *released_tags*."""
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.commands import release_reconcile as recon

    projects = load_workspace(str(root))
    ctx = WorkspaceCheckContext(
        project_root=root / "libs" / "beta-api", workspace_root=root, config={},
        projects=projects, releasables=load_releasables(str(root), projects),
    )
    target, ref_ctx, releases_dir = recon._resolve_identity(ctx)
    with patch.object(recon, "check_gh_installed", return_value=True), \
         patch.object(recon, "check_gh_auth", return_value=True), \
         patch.object(recon, "run_gh", return_value="\n".join(released_tags)):
        observation = recon.observe_world(ctx=ctx)
    explanations = recon.collect_explanations(
        [releases_dir], ref_ctx.transition_record_paths,
    )
    preview = recon.build_preview(
        observation=observation, explanations=explanations, target=target,
        ref_ctx=ref_ctx, releases_dir=releases_dir,
    )
    return preview, target, ref_ctx


def _to_do(preview):
    from rlsbl.commands.release_reconcile import STATE_ALREADY_CORRECT

    return sorted(
        f"{item.key}: {item.state}" for item in preview.items
        if item.state != STATE_ALREADY_CORRECT
    )


def _shipped_as(root, releasable, version):
    from rlsbl.release_file import (
        archived_release_path,
        get_releases_dir,
        read_release_file,
    )

    return read_release_file(archived_release_path(
        get_releases_dir(str(root), releasable_dir=get_releasable_dir(str(root), releasable)),
        version,
    )).shipped_as


class TestPastReleasesKeepTheirTags:
    """A renamed releasable's past versions are owed nothing under the new name.

    The rename's own rule: historical releases stay under the old prefix. Every
    version released before the rename shipped under the old spelling, its
    GitHub Release hangs off that tag, and consumers resolve it there. The
    reconcile must therefore find nothing to create for them -- no new-spelling
    tag and no second GitHub Release under the new name.
    """

    def _renamed(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root, create_tag=False)
        _release(root, "beta", "0.1.0")
        _release(root, "beta", "0.2.0")
        rr.rename_releasable(str(root), "beta", "gamma")
        return root

    def test_the_rename_records_the_spelling_each_past_version_shipped_under(
            self, tmp_path, monkeypatch, _gh_ok):
        root = self._renamed(tmp_path, monkeypatch)

        assert _shipped_as(root, "gamma", "0.1.0") == "beta@v0.1.0"
        assert _shipped_as(root, "gamma", "0.2.0") == "beta@v0.2.0"
        assert rr._blocking_dirty_paths(str(root)) == []

    def test_a_past_versions_primary_ref_is_the_tag_it_shipped_under(
            self, tmp_path, monkeypatch, _gh_ok):
        root = self._renamed(tmp_path, monkeypatch)
        _preview_, target, ref_ctx = _preview(
            root, "gamma", released_tags=["beta@v0.1.0", "beta@v0.2.0"],
        )

        first = target.expected_refs("0.1.0", ref_ctx)
        assert first.primary == "beta@v0.1.0"
        assert first.tags == ("beta@v0.1.0",)
        # The boundary version also carries the alias the rename pushed.
        boundary = target.expected_refs("0.2.0", ref_ctx)
        assert boundary.primary == "beta@v0.2.0"
        assert set(boundary.tags) == {"beta@v0.2.0", "gamma@v0.2.0"}

    def test_the_reconcile_creates_nothing_that_exists_under_the_old_name(
            self, tmp_path, monkeypatch, _gh_ok):
        root = self._renamed(tmp_path, monkeypatch)
        preview, _target, _ctx = _preview(
            root, "gamma", released_tags=["beta@v0.1.0", "beta@v0.2.0"],
        )

        assert _to_do(preview) == []

    def test_the_unpublished_refs_check_passes(self, tmp_path, monkeypatch, _gh_ok):
        from rlsbl import app
        from rlsbl.check_context import WorkspaceCheckContext

        root = self._renamed(tmp_path, monkeypatch)
        projects = load_workspace(str(root))
        ctx = WorkspaceCheckContext(
            project_root=root / "libs" / "beta-api", workspace_root=root,
            config={}, projects=projects,
            releasables=load_releasables(str(root), projects),
        )
        released = frozenset({"beta@v0.1.0", "beta@v0.2.0"})
        with patch("rlsbl.utils.get_github_repo", return_value="o/r"), \
             patch("rlsbl.commands.release_reconcile.list_releases",
                   return_value=(released, True)):
            result = app._check_defs["unpublished-refs"].impl(ctx)

        assert result.status == "pass", [p.text for p in result.problems]

    def test_a_release_after_the_rename_is_owed_under_the_new_name(
            self, tmp_path, monkeypatch, _gh_ok):
        root = self._renamed(tmp_path, monkeypatch)
        _release(root, "gamma", "0.3.0")
        preview, target, ref_ctx = _preview(
            root, "gamma", released_tags=["beta@v0.1.0", "beta@v0.2.0"],
        )

        assert target.expected_refs("0.3.0", ref_ctx).primary == "gamma@v0.3.0"
        assert _to_do(preview) == ["release:gamma@v0.3.0: materialize"]


class TestAnAlreadyRenamedReleasableIsRepaired:
    """A releasable renamed before the rename recorded ``shipped_as``.

    Re-running the rename is its documented heal, and it records the spelling
    on every version that shipped under the old name -- judged by the old tag
    standing at the version's own release commit, so a version released after
    the rename is left alone.
    """

    def _renamed_the_old_way(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root, create_tag=False)
        _release(root, "beta", "0.1.0")
        _release(root, "beta", "0.2.0")
        # What a rename did before it recorded shipped_as.
        rr._apply_local_rename(str(root), "beta", "gamma")
        rr._record_rename_in_transition_record(str(root), "beta", "gamma")
        rr._finish_alias_tag(
            str(root), "beta@v0.2.0", "gamma@v0.2.0", "origin",
            push_timeout=30, releasable_name="gamma",
        )
        _release(root, "gamma", "0.3.0")
        return root

    def test_the_re_run_records_the_old_spelling_on_pre_rename_versions_only(
            self, tmp_path, monkeypatch, _gh_ok):
        root = self._renamed_the_old_way(tmp_path, monkeypatch)
        assert _shipped_as(root, "gamma", "0.1.0") is None, "precondition"

        result = rr.rename_releasable(str(root), "beta", "gamma")

        assert result["mode"] == "resume"
        assert _shipped_as(root, "gamma", "0.1.0") == "beta@v0.1.0"
        assert _shipped_as(root, "gamma", "0.2.0") == "beta@v0.2.0"
        assert _shipped_as(root, "gamma", "0.3.0") is None
        assert rr._blocking_dirty_paths(str(root)) == []

    def test_after_the_repair_the_reconcile_owes_only_what_is_missing(
            self, tmp_path, monkeypatch, _gh_ok):
        root = self._renamed_the_old_way(tmp_path, monkeypatch)
        rr.rename_releasable(str(root), "beta", "gamma")

        preview, _target, _ctx = _preview(
            root, "gamma",
            released_tags=["beta@v0.1.0", "beta@v0.2.0", "gamma@v0.3.0"],
        )
        assert _to_do(preview) == []

    def test_a_second_re_run_changes_nothing(self, tmp_path, monkeypatch, _gh_ok):
        root = self._renamed_the_old_way(tmp_path, monkeypatch)
        rr.rename_releasable(str(root), "beta", "gamma")
        head = _git(root, "rev-parse", "HEAD").strip()

        rr.rename_releasable(str(root), "beta", "gamma")

        assert _git(root, "rev-parse", "HEAD").strip() == head


class TestAReRunWithTheRealSaferm:
    """saferm stages the removal of a tracked file (``git rm --cached``).

    The suite's saferm stand-in only unlinks, so a re-run over a completed
    rename never saw the staged removal of the publish cache that sync then
    regenerates byte-identically: the index held a deletion the tree did not,
    and the rename commit was refused as empty.
    """

    def test_a_re_run_after_a_completed_rename_succeeds(
            self, tmp_path, monkeypatch, _gh_ok):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root)
        rr.rename_releasable(str(root), "beta", "beta2")
        # A first re-run settles the cache: it records the hash of the router
        # the rename regenerated, so from here on sync rewrites it unchanged.
        rr.rename_releasable(str(root), "beta", "beta2")
        cache = root / WORKSPACE_DIR / "publish-cache.json"
        assert _git(root, "ls-files", str(cache.relative_to(root))), "precondition"

        def real_saferm(path, **_kwargs):
            if _git(root, "ls-files", "--", str(path), check=False):
                _git(root, "rm", "-q", "--cached", "--", str(path))
            if os.path.exists(path):
                os.unlink(path)

        monkeypatch.setattr(rr, "saferm_delete", real_saferm)
        result = rr.rename_releasable(str(root), "beta", "beta2")

        assert result["mode"] == "resume"
        assert rr._blocking_dirty_paths(str(root)) == []
        assert _git(root, "ls-files", str(cache.relative_to(root))), (
            "the publish cache is still tracked"
        )


class TestAReRunAfterALaterRelease:
    """The current version on a re-run long after the rename is a post-rename one.

    Its tag is spelled with the new name only, and it is its own release's to
    publish: the re-run neither pushes it as an alias nor records an alias of
    it to an old-spelling tag that never existed.
    """

    def test_no_alias_is_recorded_or_pushed_for_it(self, tmp_path, monkeypatch, _gh_ok):
        from rlsbl.transition_record import (
            KIND_BOUNDARY_ALIAS,
            get_transition_record_path,
            read_events,
        )

        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root, create_tag=False)
        _release(root, "beta", "0.1.0")
        rr.rename_releasable(str(root), "beta", "gamma")
        # A later release whose tag reached only the local repository.
        write_releasable_version(str(root), "gamma", "0.2.0")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "gamma v0.2.0")
        _git(root, "tag", "gamma@v0.2.0")

        result = rr.rename_releasable(str(root), "beta", "gamma")

        assert result["tag"]["status"] == "no_source_tag"
        assert not rr._tag_exists_remote(str(root), "origin", "gamma@v0.2.0")
        record = get_transition_record_path(
            str(root), releasable_dir=get_releasable_dir(str(root), "gamma"),
        )
        aliased = [
            a.aliased_tag for e in read_events(record, kinds=[KIND_BOUNDARY_ALIAS])
            for a in e.aliases
        ]
        assert "beta@v0.2.0" not in aliased


class TestTheRenameNeedsAChangelogEntry:
    """The rename commit changes every future tag of the releasable, so it is a
    user-visible change: it carries no Autogenerated trailer, changelog coverage
    asks for an entry, and the closing message prints the command that adds it.
    """

    def test_the_printed_changelog_line_covers_the_rename_commit(
            self, tmp_path, monkeypatch, _gh_ok):
        import shlex

        import rlsbl

        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        _build_monorepo(root, create_tag=False)
        _release(root, "beta", "0.1.0")

        # From a member directory, which names the releasable the check
        # answers for.
        def coverage():
            monkeypatch.chdir(root / "libs" / "beta-api")
            return rlsbl.app.test(["check", "--name", "changelog-coverage"])

        before = coverage()
        assert before.exit_code == 0, before.stdout + before.stderr

        result = rlsbl.app.test([
            "monorepo", "rename-releasable", "beta", "beta2",
            "--approve-consequential",
        ])
        assert result.exit_code == 0, result.stdout + result.stderr

        rename_sha = _git(
            root, "log", "-1", "--format=%H", "--fixed-strings",
            "--grep=monorepo: rename releasable beta -> beta2",
        ).strip()
        assert rename_sha
        body = _git(root, "log", "-1", "--format=%B", rename_sha)
        assert "Autogenerated: true" not in body

        flagged = coverage()
        assert flagged.exit_code != 0, flagged.stdout
        assert rename_sha[:7] in flagged.stdout + flagged.stderr

        line = next(
            ln.strip() for ln in result.stdout.splitlines()
            if "rlsbl changelog add" in ln
        )
        assert line.startswith("(cd "), line
        where, command = line[len("(cd "):].rstrip(")").split(" && ", 1)
        argv = shlex.split(command)[1:]
        assert argv[argv.index("--commits") + 1] == rename_sha[:12]
        assert argv[argv.index("--type") + 1] == "breaking"
        monkeypatch.chdir(root / where)
        ran = rlsbl.app.test(argv)
        assert ran.exit_code == 0, ran.stdout + ran.stderr

        cleared = coverage()
        assert cleared.exit_code == 0, cleared.stdout + cleared.stderr
