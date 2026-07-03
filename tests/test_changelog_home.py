"""Tests for the changelog home unification (rlsbl.changelog.home).

The canonical CHANGELOG.md always lives in the releasable dir for
releasable members (project root for standalone projects), PLUS a combined
root CHANGELOG.md covers all releasables of the workspace. The release
flow, `rlsbl changelog generate`, and the changelog-entry check must all
agree on these locations via the single resolver.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl import app
from rlsbl.changelog.home import (
    _strip_canonical_header,
    generate_workspace_changelog,
    get_changelog_home,
    get_workspace_changelog_path,
)
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.commands.release import run_cmd
from rlsbl.context import create_context
from rlsbl.release_file import ReleaseConfig
from rlsbl.utils import run as real_run
from rlsbl.workspace import (
    Releasable,
    get_releasable_changes_dir,
    get_releasable_dir,
    load_releasables,
    load_workspace,
    save_workspace,
    write_releasable_version,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_head(repo):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _rc(description=""):
    return ReleaseConfig(
        bump="patch",
        include=["npm"],
        exclude=[],
        description=description,
    )


def _fake_run_factory():
    def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
        if cmd == "gh":
            return ""
        if cmd == "git" and args and args[0] == "push":
            return ""
        if cmd == "git" and args and args[0] == "fetch":
            return ""
        if cmd == "git" and args and args[:2] == ["rev-list", "--count"] and any("origin/" in a for a in args):
            return "0"
        return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)
    return fake_run


_RELEASE_PATCHES_KWARGS = dict(yes=True, quiet=True)


def _run_release(member_dir, root, description=""):
    """Run a full (mocked-remote) release for the given member."""
    fake_run = _fake_run_factory()
    ctx = create_context(Path(str(member_dir)), workspace_root=Path(str(root)))
    with (
        patch("rlsbl.commands.release.check_gh_installed", return_value=True),
        patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        patch("rlsbl.commands.release.push_if_needed"),
        patch("rlsbl.commands.release.run_gh", return_value=""),
        patch("rlsbl.commands.release.run", side_effect=fake_run),
        patch("rlsbl.commands.release.remote_branch_exists", return_value=True),
    ):
        run_cmd(
            _rc(description=description),
            {"yes": True, "quiet": True, "skip-lock": True},
            ctx=ctx,
        )


def _setup_releasable_workspace(root, member_path="packages/core",
                                releasable_name="alpha"):
    """Workspace with one releasable whose sole member is *member_path*.

    The member has NO CHANGELOG.md; the canonical CHANGELOG.md lives in the
    releasable dir. Returns the member directory path.
    """
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")

    member = root / member_path if member_path != "." else root
    member.mkdir(parents=True, exist_ok=True)
    (member / "package.json").write_text(
        json.dumps({"name": "core", "version": "1.0.0"}, indent=2) + "\n"
    )
    (member / ".rlsbl").mkdir(exist_ok=True)
    (member / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False, "targets": ["npm"], "pipelines": {}}) + "\n"
    )

    project_entry = {
        "path": member_path, "name": "core", "releasable": releasable_name,
    }
    if member_path == ".":
        # Root members can't rely on the path prefix for commit scoping
        # (a "./" prefix never matches); real root-member workspaces use
        # watch globs.
        project_entry["watch"] = ["*", "**/*"]
    save_workspace(
        str(root),
        [project_entry],
        releasables=[Releasable(name=releasable_name)],
    )
    write_releasable_version(str(root), releasable_name, "1.0.0")
    changes_dir = get_releasable_changes_dir(str(root), releasable_name)
    os.makedirs(changes_dir, exist_ok=True)
    with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
        f.write("")
    rel_dir = get_releasable_dir(str(root), releasable_name)
    with open(os.path.join(rel_dir, "config.json"), "w") as f:
        f.write("{}\n")
    # Canonical CHANGELOG.md at the releasable level
    with open(os.path.join(rel_dir, "CHANGELOG.md"), "w") as f:
        f.write(
            "<!-- Generated by rlsbl from .rlsbl/changes/ — do not edit -->\n\n"
            "# Changelog\n\n## 1.0.0\n\n- No user-facing changes.\n"
        )

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    _git(root, "tag", f"{releasable_name}@v1.0.0")

    # Member-scoped feature commit
    (member / "feature.txt").write_text("new feature\n")
    _git(root, "add", os.path.join(member_path, "feature.txt")
         if member_path != "." else "feature.txt")
    _git(root, "commit", "-q", "-m", "add feature")
    feature_sha = _git_head(root)

    entry = {
        "commits": [feature_sha],
        "user_facing": True,
        "description": "**New feature.** A shiny new thing.",
        "type": "feature",
    }
    with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
        f.write(json.dumps(entry) + "\n")
    _git(root, "add", os.path.relpath(changes_dir, str(root)))
    _git(root, "commit", "-q", "-m", "changelog: add feature entry",
         "--trailer", "Autogenerated: true")

    return member


# ---------------------------------------------------------------------------
# Unit: path resolution
# ---------------------------------------------------------------------------


class TestGetChangelogHome:

    def test_standalone(self):
        assert get_changelog_home("/proj") == os.path.join("/proj", "CHANGELOG.md")

    def test_releasable(self):
        assert get_changelog_home(
            "/ws/packages/core", releasable_dir="/ws/.rlsbl-monorepo/releasables/alpha",
        ) == "/ws/.rlsbl-monorepo/releasables/alpha/CHANGELOG.md"

    def test_workspace_path(self):
        assert get_workspace_changelog_path("/ws") == os.path.join("/ws", "CHANGELOG.md")


class TestStripCanonicalHeader:

    def test_strips_comment_and_h1(self):
        content = (
            "<!-- Generated by rlsbl from .rlsbl/changes/ — do not edit -->\n\n"
            "# Changelog\n\n## 1.0.0\n\n- Thing.\n"
        )
        assert _strip_canonical_header(content) == "## 1.0.0\n\n- Thing."

    def test_no_header_passthrough(self):
        assert _strip_canonical_header("## 1.0.0\n\n- Thing.\n") == "## 1.0.0\n\n- Thing."


class TestGenerateWorkspaceChangelog:

    def test_sections_in_workspace_order(self, tmp_project):
        root = tmp_project
        save_workspace(
            str(root),
            [
                {"path": "packages/b", "name": "b", "releasable": "beta"},
                {"path": "packages/a", "name": "a", "releasable": "alpha"},
            ],
            releasables=[Releasable(name="beta"), Releasable(name="alpha")],
        )
        for name, version in (("beta", "2.0.0"), ("alpha", "1.0.0")):
            rel_dir = get_releasable_dir(str(root), name)
            os.makedirs(rel_dir, exist_ok=True)
            with open(os.path.join(rel_dir, "CHANGELOG.md"), "w") as f:
                f.write(
                    "<!-- Generated by rlsbl — do not edit -->\n\n"
                    f"# Changelog\n\n## {version}\n\n- Change in {name}.\n"
                )

        content = generate_workspace_changelog(str(root))

        root_file = Path(get_workspace_changelog_path(str(root)))
        assert root_file.is_file()
        assert root_file.read_text() == content
        # Marked as generated
        assert content.startswith("<!--")
        # Sections in workspace.toml order: beta first, then alpha
        assert "# beta" in content and "# alpha" in content
        assert content.index("# beta") < content.index("# alpha")
        assert "## 2.0.0" in content
        assert "- Change in beta." in content
        assert "- Change in alpha." in content
        # No nested canonical headers leaked into the combined file
        assert "# Changelog" not in content

    def test_missing_canonical_omitted(self, tmp_project):
        root = tmp_project
        save_workspace(
            str(root),
            [{"path": "packages/a", "name": "a", "releasable": "alpha"}],
            releasables=[Releasable(name="alpha")],
        )
        content = generate_workspace_changelog(str(root))
        assert "# alpha" not in content

    def test_unchanged_content_not_rewritten(self, tmp_project):
        root = tmp_project
        save_workspace(
            str(root),
            [{"path": "packages/a", "name": "a", "releasable": "alpha"}],
            releasables=[Releasable(name="alpha")],
        )
        rel_dir = get_releasable_dir(str(root), "alpha")
        os.makedirs(rel_dir, exist_ok=True)
        with open(os.path.join(rel_dir, "CHANGELOG.md"), "w") as f:
            f.write("# Changelog\n\n## 1.0.0\n\n- X.\n")
        generate_workspace_changelog(str(root))
        out = Path(get_workspace_changelog_path(str(root)))
        mtime = out.stat().st_mtime_ns
        generate_workspace_changelog(str(root))
        assert out.stat().st_mtime_ns == mtime


# ---------------------------------------------------------------------------
# E2E: releasable release writes canonical + combined root CHANGELOG.md
# ---------------------------------------------------------------------------


class TestReleasableReleaseChangelogHome:

    def test_release_produces_canonical_and_root_files(self, tmp_project):
        """A releasable release writes the canonical CHANGELOG.md in the
        releasable dir AND the combined root CHANGELOG.md; they agree, and
        the representative member directory gets NO CHANGELOG.md."""
        core = _setup_releasable_workspace(tmp_project)

        _run_release(core, tmp_project)

        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        canonical = Path(rel_dir) / "CHANGELOG.md"
        root_combined = Path(str(tmp_project)) / "CHANGELOG.md"

        assert canonical.is_file(), "canonical CHANGELOG.md must live in the releasable dir"
        canonical_text = canonical.read_text()
        assert "## 1.0.1" in canonical_text
        assert "A shiny new thing." in canonical_text

        assert root_combined.is_file(), "combined root CHANGELOG.md must exist"
        root_text = root_combined.read_text()
        assert root_text.startswith("<!--"), "root file must be marked generated"
        assert "# alpha" in root_text
        assert "## 1.0.1" in root_text
        assert "A shiny new thing." in root_text

        # The two files agree on the version body
        assert _strip_canonical_header(canonical_text) in root_text

        # ZERO pollution of the representative member
        assert not (core / "CHANGELOG.md").exists(), (
            "releasable release must not write CHANGELOG.md into the "
            "representative member's directory"
        )

        # Both files are committed (clean tree)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert status == "", f"release must leave a clean tree, got: {status}"

    def test_changelog_generate_after_release_is_noop(self, tmp_project, monkeypatch):
        """`rlsbl changelog generate` right after a release regenerates the
        exact same content at the exact same paths."""
        core = _setup_releasable_workspace(tmp_project)
        _run_release(core, tmp_project)

        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        canonical = Path(rel_dir) / "CHANGELOG.md"
        root_combined = Path(str(tmp_project)) / "CHANGELOG.md"
        before_canonical = canonical.read_text()
        before_root = root_combined.read_text()

        from rlsbl.commands.changelog_cmd import cmd_generate
        monkeypatch.chdir(core)
        cmd_generate({"auto-commit": False}, project_root=str(core))

        assert canonical.read_text() == before_canonical
        assert root_combined.read_text() == before_root
        # No new CHANGELOG.md appeared in the member dir
        assert not (core / "CHANGELOG.md").exists()

    def test_changelog_generate_auto_commit_releasable(self, tmp_project, monkeypatch):
        """`rlsbl changelog generate` with auto-commit in releasable mode
        commits the canonical and combined root files (leaves a clean tree)."""
        core = _setup_releasable_workspace(tmp_project)
        _run_release(core, tmp_project)

        # Dirty the canonical file so generate has something to regenerate
        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        canonical = Path(rel_dir) / "CHANGELOG.md"
        canonical.write_text("stale\n")
        subprocess.run(
            ["git", "commit", "-q", "-am", "stale changelog"],
            cwd=str(tmp_project), check=True,
        )

        from rlsbl.commands.changelog_cmd import cmd_generate
        monkeypatch.chdir(core)
        cmd_generate({"auto-commit": True}, project_root=str(core))

        assert "## 1.0.1" in canonical.read_text()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert status == "", (
            f"auto-commit must leave a clean tree, got: {status}"
        )

    def test_changelog_entry_check_passes_new_layout(self, tmp_project):
        """The changelog-entry check reads the canonical releasable
        CHANGELOG.md and passes after a release."""
        core = _setup_releasable_workspace(tmp_project)
        _run_release(core, tmp_project)

        projects = load_workspace(str(tmp_project))
        releasables = load_releasables(str(tmp_project), projects=projects)
        ctx = WorkspaceCheckContext(
            project_root=Path(str(core)),
            workspace_root=Path(str(tmp_project)),
            config={"private": False, "targets": ["npm"]},
            projects=projects,
            graph=None,
            releasables=releasables,
        )
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "pass", result.message

    def test_changelog_amend_regenerates_at_home(self, tmp_project, monkeypatch):
        """`rlsbl changelog amend` on a released version regenerates the
        canonical releasable CHANGELOG.md (and root combined), never a
        member-dir CHANGELOG.md."""
        core = _setup_releasable_workspace(tmp_project)
        _run_release(core, tmp_project)

        # A new member-scoped commit to amend into the released version
        (core / "hotfix.txt").write_text("hotfix\n")
        _git(tmp_project, "add", "packages/core/hotfix.txt")
        _git(tmp_project, "commit", "-q", "-m", "hotfix")
        sha = _git_head(tmp_project)

        from rlsbl.commands.changelog_cmd import cmd_amend
        monkeypatch.chdir(core)
        with patch("rlsbl.commands.changelog_cmd._sync_github_release"):
            cmd_amend(
                {
                    "version": "1.0.1",
                    "commits": sha,
                    "description": "**Amended fix.** Hot stuff.",
                    "type": "fix",
                    "user-facing": True,
                    "validate-hashes": True,
                },
                project_root=str(core),
            )

        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        canonical = (Path(rel_dir) / "CHANGELOG.md").read_text()
        assert "Amended fix." in canonical
        root_combined = (Path(str(tmp_project)) / "CHANGELOG.md").read_text()
        assert "Amended fix." in root_combined
        assert not (core / "CHANGELOG.md").exists(), (
            "amend must not write CHANGELOG.md into the member directory"
        )

    def test_changelog_edit_regenerates_at_home(self, tmp_project, monkeypatch):
        """`rlsbl changelog edit` on a released entry regenerates the
        canonical releasable CHANGELOG.md (and root combined), never a
        member-dir CHANGELOG.md."""
        core = _setup_releasable_workspace(tmp_project)
        _run_release(core, tmp_project)

        # The released 1.0.1.jsonl contains the feature entry; edit it
        changes_dir = get_releasable_changes_dir(str(tmp_project), "alpha")
        entry = json.loads(
            (Path(changes_dir) / "1.0.1.jsonl").read_text().splitlines()[0]
        )
        sha = entry["commits"][0]

        from rlsbl.commands.changelog_cmd import cmd_edit
        monkeypatch.chdir(core)
        with patch("rlsbl.commands.changelog_cmd._sync_github_release"):
            cmd_edit(
                {
                    "commits": sha,
                    "description": "**Edited feature.** Even shinier.",
                    "type": "",
                    "user-facing": None,
                    "auto-commit": True,
                },
                project_root=str(core),
            )

        rel_dir = get_releasable_dir(str(tmp_project), "alpha")
        canonical = (Path(rel_dir) / "CHANGELOG.md").read_text()
        assert "Edited feature." in canonical
        root_combined = (Path(str(tmp_project)) / "CHANGELOG.md").read_text()
        assert "Edited feature." in root_combined
        assert not (core / "CHANGELOG.md").exists(), (
            "edit must not write CHANGELOG.md into the member directory"
        )

    def test_root_member_releasable_sane_layout(self, tmp_project):
        """orxtra-shaped workspace: the sole member's path is '.'. The
        canonical file in the releasable dir and the combined root file
        coexist without duplication weirdness."""
        member = _setup_releasable_workspace(
            tmp_project, member_path=".", releasable_name="solo",
        )
        assert member == tmp_project

        _run_release(member, tmp_project)

        rel_dir = get_releasable_dir(str(tmp_project), "solo")
        canonical = Path(rel_dir) / "CHANGELOG.md"
        root_combined = Path(str(tmp_project)) / "CHANGELOG.md"

        assert canonical.is_file()
        assert root_combined.is_file()
        canonical_text = canonical.read_text()
        root_text = root_combined.read_text()
        assert "## 1.0.1" in canonical_text
        assert "# solo" in root_text
        assert "## 1.0.1" in root_text
        # Root file is the combined format, not a stale copy of the canonical
        assert root_text.startswith("<!--")
        assert "# Changelog" not in root_text
        # Exactly one section for the sole releasable
        assert root_text.count("# solo") == 1
