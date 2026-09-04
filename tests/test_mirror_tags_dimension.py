"""The mirror reconciler's second dimension: the released versions' tags.

A mirror can be perfectly converged on ``main`` and still carry none of the
tags its releasable's release record records. The preview names each missing version,
and an apply materializes it -- the tag at the subtree split of that version's
release record anchor, and the mirror's own GitHub Release beside it.

Every remote here is a local bare repository reached over ``file://``.
"""

import json
import subprocess

import pytest

from conftest import archive_release, make_workspace

from rlsbl.commands.monorepo import mirror_cmd
from rlsbl.commands.monorepo.mirror_cmd import _cmd_mirror, observe_tags
from rlsbl.mirror_publication import (
    mirror_tag,
    parse_ls_remote,
    remote_refs,
    remote_tag_commits,
    split_commit_for,
)
from rlsbl.targets import TARGETS
from rlsbl.workspace_types import get_releasable_dir


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _bare(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare"], cwd=str(path), check=True)
    return str(path)


def _monorepo(root, remote, *, name="mylib", path="mylib"):
    """A monorepo with one mirrored releasable and a released 1.0.0."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.local")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")

    member = root / path
    (member / ".rlsbl").mkdir(parents=True, exist_ok=True)
    (member / "package.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}) + "\n"
    )
    (member / ".rlsbl" / "config.json").write_text(
        json.dumps({"targets": ["npm"], "publish_mode": "none"}, indent=2) + "\n"
    )
    make_workspace(
        str(root),
        [{"path": path, "name": name, "releasable": name}],
        releasables=[{"name": name, "subtree_remote": remote}],
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "release 1.0.0")
    anchor = _git(root, "rev-parse", "HEAD")

    state = get_releasable_dir(str(root), name)
    (root / "x").write_text("")  # keep the tree non-empty for later commits
    archive_release(f"{state}/releases", "1.0.0", anchor)
    changes = f"{state}/changes"
    import os

    os.makedirs(changes, exist_ok=True)
    with open(f"{changes}/1.0.0.md", "w", encoding="utf-8") as f:
        f.write("### Features\n- the first release\n")
    return anchor


class _FakeGh:
    def __init__(self):
        self.calls = []
        self.bodies = {}

    def __call__(self, args, config=None):
        self.calls.append(list(args))
        if args[:2] == ["release", "view"]:
            if args[2] not in self.bodies:
                raise RuntimeError("not found")
            return self.bodies[args[2]]
        if args[:2] in (["release", "create"], ["release", "edit"]):
            path = args[args.index("--notes-file") + 1]
            with open(path, encoding="utf-8") as f:
                self.bodies[args[2]] = f.read()
        return ""


def _tag_plans(root, remote, path="mylib", name="mylib"):
    state = get_releasable_dir(str(root), name)
    return observe_tags(
        remote, str(root), path,
        releases_dir=f"{state}/releases",
        changes_dir=f"{state}/changes",
        tag_of=lambda v: mirror_tag(v, target=TARGETS["npm"]),
        remote_refs_text=subprocess.run(
            ["git", "ls-remote", remote], cwd=str(root),
            capture_output=True, text=True, check=True,
        ).stdout,
    )


class TestObserveTags:
    def test_a_released_version_the_mirror_lacks_is_materializable(self, tmp_path):
        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        anchor = _monorepo(root, remote)

        [plan] = _tag_plans(root, remote)
        assert plan.version == "1.0.0"
        assert plan.tag == "v1.0.0"
        assert plan.state == "materialize"
        assert plan.anchor_sha == anchor
        assert plan.split_sha == split_commit_for(str(root), "mylib", anchor)
        assert "the first release" in plan.notes

    def test_a_tag_the_mirror_carries_is_present(self, tmp_path):
        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        anchor = _monorepo(root, remote)
        split = split_commit_for(str(root), "mylib", anchor)
        _git(root, "push", "-q", remote, f"{split}:refs/tags/v1.0.0")

        [plan] = _tag_plans(root, remote)
        assert plan.state == "present"
        assert plan.remote_commit == split

    def test_an_unanchored_version_is_reported_never_guessed(self, tmp_path):
        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        _monorepo(root, remote)
        state = get_releasable_dir(str(root), "mylib")
        archive_release(f"{state}/releases", "0.9.0", "", unanchorable=True)

        plans = {p.version: p for p in _tag_plans(root, remote)}
        assert plans["0.9.0"].state == "unanchored"
        assert plans["0.9.0"].split_sha is None

    def test_an_anchor_with_no_mirror_commit_is_unanchored_not_fatal(
        self, tmp_path,
    ):
        """An anchor that predates the member's directory must not kill the run.

        A version absorbed from an era before this subtree existed has a real
        release record anchor, but no ``git subtree split`` of the member path can
        answer for that commit. That is one version's problem: it becomes an
        ``unanchored`` item carrying the reason, and every other version -- and
        the branch itself -- is judged as usual.
        """
        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        _monorepo(root, remote)

        # A commit from before the member's directory existed: no split of
        # 'mylib' can answer for it.
        empty_tree = subprocess.run(
            ["git", "mktree"], cwd=str(root), input="",
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        absorbed = _git(root, "commit-tree", empty_tree, "-m", "pre-mylib era")

        state = get_releasable_dir(str(root), "mylib")
        archive_release(f"{state}/releases", "0.9.0", absorbed)

        plans = {p.version: p for p in _tag_plans(root, remote)}
        assert plans["0.9.0"].state == "unanchored"
        assert plans["0.9.0"].split_sha is None
        assert plans["0.9.0"].anchor_sha == absorbed
        assert plans["0.9.0"].reason, "the reason the split failed is reported"
        # The version that CAN be derived is unaffected.
        assert plans["1.0.0"].state == "materialize"

    def test_a_releasable_with_no_archives_has_no_tag_plans(self, tmp_path):
        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        _monorepo(root, remote)
        import shutil

        shutil.rmtree(f"{get_releasable_dir(str(root), 'mylib')}/releases")
        assert _tag_plans(root, remote) == []


class TestThroughTheCommand:
    def test_the_plan_names_the_missing_tag(self, tmp_path, capsys, monkeypatch):
        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        _monorepo(root, remote)
        monkeypatch.setattr(
            mirror_cmd, "validate_subtree_remote_ssh_host",
            lambda remote, root: None,
        )

        _cmd_mirror({"project": "mylib", "dry-run": True}, project_root=root)
        out = capsys.readouterr().out
        assert "tag:v1.0.0" in out
        assert "missing the tag for released 1.0.0" in out
        # ...and nothing was written to the mirror.
        assert remote_tag_commits(remote_refs(remote, str(root))) == {}

    def test_apply_materializes_the_tag_and_the_release(
        self, tmp_path, capsys, monkeypatch,
    ):
        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        anchor = _monorepo(root, remote)
        gh = _FakeGh()
        monkeypatch.setattr(
            mirror_cmd, "validate_subtree_remote_ssh_host",
            lambda remote, root: None,
        )
        monkeypatch.setattr(
            "rlsbl.utils.run_gh_unscoped", lambda args, **kwargs: gh(args),
        )

        _cmd_mirror({"project": "mylib", "dry-run": False}, project_root=root)

        split = split_commit_for(str(root), "mylib", anchor)
        tags = remote_tag_commits(remote_refs(remote, str(root)))
        assert tags == {"v1.0.0": split}
        assert f"<!-- rlsbl-ci-sha: {split} -->" in gh.bodies["v1.0.0"]
        assert "the first release" in gh.bodies["v1.0.0"]

    def test_rerunning_reports_the_tag_as_present(
        self, tmp_path, capsys, monkeypatch,
    ):
        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        _monorepo(root, remote)
        gh = _FakeGh()
        monkeypatch.setattr(
            mirror_cmd, "validate_subtree_remote_ssh_host",
            lambda remote, root: None,
        )
        monkeypatch.setattr(
            "rlsbl.utils.run_gh_unscoped", lambda args, **kwargs: gh(args),
        )
        _cmd_mirror({"project": "mylib", "dry-run": False}, project_root=root)
        capsys.readouterr()
        _cmd_mirror({"project": "mylib", "dry-run": True}, project_root=root)
        out = capsys.readouterr().out
        assert "the mirror carries v1.0.0" in out


    def test_one_underivable_version_does_not_stop_the_branch(
        self, tmp_path, capsys, monkeypatch,
    ):
        """A version the split cannot answer for costs that version only.

        The anchor of an absorbed-era release predates the member's directory,
        so no subtree split of that path can name a mirror commit for it. That
        used to raise out of observation, and with it went the branch: nothing
        converged, and the command reported the split failure as if the whole
        mirror were unreachable.
        """
        from rlsbl.commands.monorepo.mirror_cmd import observe

        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        _monorepo(root, remote)
        empty_tree = subprocess.run(
            ["git", "mktree"], cwd=str(root), input="",
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        absorbed = _git(root, "commit-tree", empty_tree, "-m", "pre-mylib era")
        archive_release(
            f"{get_releasable_dir(str(root), 'mylib')}/releases",
            "0.9.0", absorbed,
        )
        gh = _FakeGh()
        monkeypatch.setattr(
            mirror_cmd, "validate_subtree_remote_ssh_host",
            lambda remote, root: None,
        )
        monkeypatch.setattr(
            "rlsbl.utils.run_gh_unscoped", lambda args, **kwargs: gh(args),
        )

        _cmd_mirror({"project": "mylib", "dry-run": True}, project_root=root)
        out = capsys.readouterr().out
        assert "tag:v0.9.0" in out and "unanchored" in out
        assert "tag:v1.0.0" in out

        _cmd_mirror({"project": "mylib", "dry-run": False}, project_root=root)
        assert observe(remote, str(root), "mylib").state == "converged"
        assert "v1.0.0" in remote_tag_commits(remote_refs(remote, str(root)))

    def test_the_release_notes_file_is_written_beside_the_release_state(
        self, tmp_path, monkeypatch,
    ):
        """Not in the directory the command was invoked from.

        A stray ``.rlsbl-notes-*.tmp`` in the operator's working tree is a file
        the next `git status` reports; the mirror's Release body belongs beside
        the release state the version came from.
        """
        import os

        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        _monorepo(root, remote)
        notes_paths = []

        class _RecordingGh(_FakeGh):
            def __call__(self, args, config=None):
                if "--notes-file" in args:
                    notes_paths.append(args[args.index("--notes-file") + 1])
                return super().__call__(args, config=config)

        gh = _RecordingGh()
        monkeypatch.setattr(
            mirror_cmd, "validate_subtree_remote_ssh_host",
            lambda remote, root: None,
        )
        monkeypatch.setattr(
            "rlsbl.utils.run_gh_unscoped", lambda args, **kwargs: gh(args),
        )

        _cmd_mirror({"project": "mylib", "dry-run": False}, project_root=root)

        assert notes_paths
        expected = os.path.join(
            get_releasable_dir(str(root), "mylib"), "releases",
        )
        for path in notes_paths:
            assert os.path.realpath(os.path.dirname(path)) == os.path.realpath(
                expected
            ), path

    def test_the_converged_mirror_carries_no_publish_workflow(
        self, tmp_path, monkeypatch,
    ):
        """A mirror's Releases come from the release flow, not its own CI."""
        import os

        remote = _bare(tmp_path / "mirror.git")
        root = tmp_path / "mono"
        _monorepo(root, remote)
        gh = _FakeGh()
        monkeypatch.setattr(
            mirror_cmd, "validate_subtree_remote_ssh_host",
            lambda remote, root: None,
        )
        monkeypatch.setattr(
            "rlsbl.utils.run_gh_unscoped", lambda args, **kwargs: gh(args),
        )
        _cmd_mirror({"project": "mylib", "dry-run": False}, project_root=root)

        check = tmp_path / "check"
        subprocess.run(
            ["git", "clone", "-q", remote, str(check)], check=True,
        )
        workflows = check / ".github" / "workflows"
        assert workflows.is_dir(), "the mirror scaffold did produce workflows"
        assert not os.path.exists(workflows / "publish.yml")
        assert (check / ".rlsbl" / "config.json").is_file()


class TestScaffoldOwnedSet:
    def test_identity_manifests_are_scaffold_owned_on_a_mirror(self):
        from rlsbl.commands.monorepo.mirror_cmd import scaffold_owned_files
        from rlsbl.targets import mirror_identity_manifests

        owned = scaffold_owned_files()
        assert mirror_identity_manifests() <= owned
        assert "go.mod" in owned

    def test_the_pinned_set_is_still_in_it(self):
        from rlsbl.commands.monorepo.mirror_cmd import (
            SCAFFOLD_OWNED_FILES,
            scaffold_owned_files,
        )

        assert SCAFFOLD_OWNED_FILES <= scaffold_owned_files()


class TestMirrorScaffoldDropsPublish:
    def test_the_copied_config_declares_publish_mode_none(self, tmp_path):
        """A mirror never publishes itself: its Releases come from the release
        flow, so its scaffold gets no publish workflow."""
        import os

        from rlsbl.commands.monorepo import mirror_cmd as mc

        sub_config = tmp_path / "member" / ".rlsbl" / "config.json"
        sub_config.parent.mkdir(parents=True)
        sub_config.write_text(
            json.dumps({"targets": ["npm"], "publish_mode": "ci"}) + "\n"
        )
        clone = tmp_path / "clone"
        clone.mkdir()

        seen = {}

        def fake_run(argv, **kwargs):
            seen["config"] = json.loads(
                (clone / ".rlsbl" / "config.json").read_text()
            )

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""

            return _R()

        import rlsbl.effects as effects

        original = effects.run
        effects.run = fake_run
        try:
            mc._run_scaffold(str(clone), str(sub_config), "file:///nowhere")
        finally:
            effects.run = original

        assert seen["config"]["publish_mode"] == "none"
        assert os.path.isfile(clone / ".rlsbl" / "config.json")


class TestGoIdentityRewrite:
    def test_a_go_mirror_moves_its_module_path(self, tmp_path):
        clone = tmp_path / "clone"
        (clone / "internal").mkdir(parents=True)
        (clone / "go.mod").write_text(
            "module github.com/o/mono/packages/lib\n\ngo 1.22\n"
        )
        (clone / "main.go").write_text(
            'package main\n\nimport "github.com/o/mono/packages/lib/internal"\n'
        )
        (clone / "internal" / "x.go").write_text("package internal\n")

        rewritten = TARGETS["go"].rewrite_mirror_identity(
            str(clone), "git@github.com:o/lib.git",
        )
        assert "go.mod" in rewritten
        assert "module github.com/o/lib\n" in (clone / "go.mod").read_text()
        assert 'import "github.com/o/lib/internal"' in (
            clone / "main.go"
        ).read_text()

    def test_a_remote_with_no_module_host_is_a_hard_error(self, tmp_path):
        from rlsbl.commands.rewrite.go_module_path import GoModuleRewriteError

        clone = tmp_path / "clone"
        clone.mkdir()
        (clone / "go.mod").write_text("module github.com/o/mono/packages/lib\n")

        with pytest.raises(GoModuleRewriteError, match="no module host"):
            TARGETS["go"].rewrite_mirror_identity(
                str(clone), f"file://{tmp_path}/mirror.git",
            )

    def test_a_clone_with_no_module_directive_is_a_hard_error(self, tmp_path):
        from rlsbl.commands.rewrite.go_module_path import GoModuleRewriteError

        clone = tmp_path / "clone"
        clone.mkdir()
        with pytest.raises(GoModuleRewriteError, match="no go.mod"):
            TARGETS["go"].rewrite_mirror_identity(
                str(clone), "git@github.com:o/lib.git",
            )

    def test_an_already_correct_module_path_rewrites_nothing(self, tmp_path):
        clone = tmp_path / "clone"
        clone.mkdir()
        (clone / "go.mod").write_text("module github.com/o/lib\n")
        assert TARGETS["go"].rewrite_mirror_identity(
            str(clone), "https://github.com/o/lib.git",
        ) == []


def test_peeled_tag_refs_are_what_the_comparison_uses():
    """An annotated tag's ref line names the tag object, not the commit."""
    refs = parse_ls_remote(
        "tagobj\trefs/tags/v1.0.0\ncommit\trefs/tags/v1.0.0^{}\n"
    )
    assert remote_tag_commits(refs) == {"v1.0.0": "commit"}
