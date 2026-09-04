"""A releasable that owns the repository root, and the tags it produces.

A root member's releases are commonly tagged the way a standalone repository's
are (``v1.2.3``), because the repository often WAS one.  That is exactly the
case the workspace default ``{name}@v{version}`` gets wrong, so a releasable
owning the root has to declare its ``tag_format`` and the loader refuses one
that does not.

For that refusal to be possible, absence has to survive loading:
``Releasable.tag_format`` carries the DECLARED value or
:data:`~rlsbl.workspace_types.TAG_FORMAT_ABSENT`, and
``effective_tag_format`` is what resolves absence to the workspace scheme.
This file pins both halves, then follows a bare-version root releasable
through the consumers that parse tag schemes.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from conftest import make_workspace
from githarness import git
from rlsbl.tag_glob import (
    TagMode,
    parse_version_tag,
    releasable_tag_glob,
    resolve_monorepo_tag_glob,
)
from rlsbl.workspace import (
    DEFAULT_TAG_FORMAT,
    STANDALONE_TAG_FORMAT,
    TAG_FORMAT_ABSENT,
    Releasable,
    load_releasables,
    load_workspace,
    save_workspace,
    write_releasable_version,
)
from rlsbl.workspace_types import WORKSPACE_DIR, WORKSPACE_FILE


# ---------------------------------------------------------------------------
# The sentinel and its round trip
# ---------------------------------------------------------------------------


class TestAbsenceIsCarried:

    def test_an_undeclared_format_loads_as_absent(self, tmp_path):
        make_workspace(str(tmp_path), [{"path": "pkg", "name": "pkg", "releasable": "pkg"}],
                       releasables=[Releasable(name="pkg")])
        rel = load_releasables(str(tmp_path))[0]
        assert rel.tag_format is TAG_FORMAT_ABSENT
        assert not rel.declares_tag_format
        assert rel.effective_tag_format == DEFAULT_TAG_FORMAT

    def test_a_declared_format_loads_as_declared(self, tmp_path):
        make_workspace(
            str(tmp_path), [{"path": "pkg", "name": "pkg", "releasable": "pkg"}],
            releasables=[Releasable(name="pkg", tag_format=STANDALONE_TAG_FORMAT)],
        )
        rel = load_releasables(str(tmp_path))[0]
        assert rel.declares_tag_format
        assert rel.tag_format == STANDALONE_TAG_FORMAT

    def test_declaring_the_default_is_not_absence(self, tmp_path):
        """The two tag identically, and only one wrote a line to preserve."""
        make_workspace(
            str(tmp_path), [{"path": "pkg", "name": "pkg", "releasable": "pkg"}],
            releasables=[Releasable(name="pkg", tag_format=DEFAULT_TAG_FORMAT)],
        )
        rel = load_releasables(str(tmp_path))[0]
        assert rel.declares_tag_format
        assert rel.effective_tag_format == DEFAULT_TAG_FORMAT


class TestSaveWorkspaceRoundTrip:

    def _ws_text(self, root):
        return (Path(root) / WORKSPACE_DIR / WORKSPACE_FILE).read_text(encoding="utf-8")

    def test_absence_stays_absent_across_a_save(self, tmp_path):
        make_workspace(str(tmp_path), [{"path": "pkg", "name": "pkg", "releasable": "pkg"}],
                       releasables=[Releasable(name="pkg")])
        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects)
        save_workspace(str(tmp_path), projects, releasables=releasables)

        assert "tag_format" not in self._ws_text(tmp_path)
        assert not load_releasables(str(tmp_path))[0].declares_tag_format

    def test_an_explicit_default_survives_a_save(self, tmp_path):
        """Rewriting must not delete an operator-written line."""
        make_workspace(
            str(tmp_path), [{"path": "pkg", "name": "pkg", "releasable": "pkg"}],
            releasables=[Releasable(name="pkg", tag_format=DEFAULT_TAG_FORMAT)],
        )
        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects)
        save_workspace(str(tmp_path), projects, releasables=releasables)

        assert "tag_format" in self._ws_text(tmp_path)
        assert load_releasables(str(tmp_path))[0].declares_tag_format

    def test_a_bare_version_format_survives_a_save(self, tmp_path):
        make_workspace(
            str(tmp_path),
            [{"path": ".", "name": "root", "releasable": "app"}],
            releasables=[Releasable(name="app", tag_format=STANDALONE_TAG_FORMAT)],
        )
        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects)
        save_workspace(str(tmp_path), projects, releasables=releasables)

        reloaded = load_releasables(str(tmp_path))[0]
        assert reloaded.tag_format == STANDALONE_TAG_FORMAT


class TestTheRootReleasableMustDeclareOne:

    def test_the_loader_refuses_an_undeclared_root_releasable(self, tmp_path):
        from rlsbl.errors import WorkspaceError

        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir(parents=True)
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[releasables]]\nname = "app"\n\n'
            '[[projects]]\npath = "."\nname = "root"\nreleasable = "app"\n',
            encoding="utf-8",
        )
        with pytest.raises(WorkspaceError, match="declares no tag_format"):
            load_workspace(str(tmp_path))


# ---------------------------------------------------------------------------
# A bare-version root releasable, end to end
# ---------------------------------------------------------------------------


def _bare_version_root_workspace(root, version="1.2.3"):
    """A repository whose root member is released as ``v{version}``."""
    root = Path(root)
    (root / "package.json").write_text(
        json.dumps({"name": "app", "version": version}, indent=2) + "\n"
    )
    (root / ".rlsbl").mkdir(exist_ok=True)
    (root / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "none", "targets": ["npm"]}) + "\n"
    )
    make_workspace(
        str(root),
        [{"path": ".", "name": "root", "releasable": "app"}],
        releasables=[Releasable(name="app", tag_format=STANDALONE_TAG_FORMAT)],
    )
    write_releasable_version(str(root), "app", version)
    changes = root / WORKSPACE_DIR / "releasables" / "app" / "changes"
    changes.mkdir(parents=True, exist_ok=True)
    (changes / "unreleased.jsonl").write_text("")
    return root


class TestBareVersionRootReleasable:

    def test_the_tag_glob_is_the_standalone_one(self, tmp_path):
        _bare_version_root_workspace(tmp_path)
        projects = load_workspace(str(tmp_path))
        rel = load_releasables(str(tmp_path), projects)[0]
        root_member = [p for p in projects if p["path"] == "."][0]

        assert releasable_tag_glob(rel.effective_tag_format, rel.name) == "v*"
        assert resolve_monorepo_tag_glob(
            root_member, str(tmp_path), releasable=rel,
        ) == "v*"

    def test_its_tags_parse_as_a_known_scheme(self):
        parsed = parse_version_tag("v1.2.3", mode=TagMode.FINAL_ONLY)
        assert parsed is not None
        assert parsed.version == "1.2.3"
        assert parsed.scheme == "standalone"

    def test_the_glob_does_not_reach_the_go_path_scheme(self, tmp_path):
        """A Go root member never produces the degenerate ``./v*`` glob.

        The root member's path is ``.``, and Go's monorepo glob is
        path-based, so falling through to the target would derive a pattern
        matching nothing. The releasable's declared format answers first.
        """
        root = _bare_version_root_workspace(tmp_path)
        (root / "go.mod").write_text("module example.com/app\n\ngo 1.22\n")
        projects = load_workspace(str(root))
        rel = load_releasables(str(root), projects)[0]
        root_member = [p for p in projects if p["path"] == "."][0]

        glob = resolve_monorepo_tag_glob(root_member, str(root), releasable=rel)
        assert glob == "v*"
        assert "./" not in glob

    def test_the_router_tag_prefix_comes_from_the_releasable(self, tmp_path):
        """The publish router's startsWith prefix, for a root member with Go."""
        from rlsbl.commands.monorepo.sync import _get_monorepo_tag_prefix

        root = _bare_version_root_workspace(tmp_path)
        (root / "go.mod").write_text("module example.com/app\n\ngo 1.22\n")
        projects = load_workspace(str(root))
        releasables = load_releasables(str(root), projects)
        root_member = [p for p in projects if p["path"] == "."][0]

        prefix = _get_monorepo_tag_prefix(root_member, str(root), releasables)
        assert prefix == "v"

    def test_status_reports_the_release(self, tmp_path, capsys):
        """`monorepo status` names the releasable's release, not "(none)".

        The row reads the releasable's OWN archive directory; a bare-version
        releasable used to be resolved through the workspace default tag glob
        (``{name}@v{version}``), which matched nothing and reported it as never
        released.
        """
        from conftest import archive_release
        from rlsbl.commands.monorepo import _cmd_status

        root = _bare_version_root_workspace(tmp_path)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@test.local")
        git(root, "config", "user.name", "Test")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "initial")
        git(root, "tag", "v1.2.3")
        archive_release(
            root / ".rlsbl-monorepo" / "releasables" / "app" / "releases",
            "1.2.3", git(root, "rev-parse", "HEAD"),
        )

        cwd = os.getcwd()
        os.chdir(root)
        try:
            _cmd_status({}, project_root=".")
        finally:
            os.chdir(cwd)

        out = capsys.readouterr().out
        rows = [line for line in out.splitlines() if line.startswith("app")]
        assert rows, f"no row for the releasable in:\n{out}"
        assert "1.2.3" in rows[0], rows
        assert "(none)" not in rows[0], rows

    def test_coverage_release_commits_on_the_bare_version_tag(self, tmp_path, capsys):
        """The unreleased range starts at ``v1.2.3``, not at the repo's first commit."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.checks._common import _get_all_changelog_contexts

        root = _bare_version_root_workspace(tmp_path)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@test.local")
        git(root, "config", "user.name", "Test")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "initial")
        git(root, "tag", "v1.2.3")

        projects = load_workspace(str(root))
        ctx = WorkspaceCheckContext(
            project_root=root,
            workspace_root=root,
            config={},
            projects=projects,
            graph=None,
            releasables=load_releasables(str(root), projects),
        )
        contexts = _get_all_changelog_contexts(ctx)
        assert len(contexts) == 1
        _changes_dir, tag_glob, scope, _entries = contexts[0]
        assert tag_glob == "v*"
        # The root member owns the residual, so the whole repository is in
        # this releasable's changelog scope.
        assert scope.claims("README.md")
        assert scope.claims("src/main.go")

    def test_the_unreleased_range_uses_that_release(self, tmp_path):
        """The bare-version releasable's own archive bounds its range.

        This pinned ``git describe --match 'v*'`` finding ``v1.2.3``; it now
        pins the archived release for 1.2.3 doing the same job, so the range
        starts at the commit that release shipped from.
        """
        from conftest import archive_release, release_record_dir
        from rlsbl.release_record import unreleased_range

        root = _bare_version_root_workspace(tmp_path)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@test.local")
        git(root, "config", "user.name", "Test")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "initial")
        git(root, "tag", "v1.2.3")
        released = git(root, "rev-parse", "HEAD")
        releases = release_record_dir(None, releasable_dir=(
            root / ".rlsbl-monorepo" / "releasables" / "root"
        ))
        archive_release(releases, "1.2.3", released)
        (root / "feature.txt").write_text("x\n")
        git(root, "add", "feature.txt")
        git(root, "commit", "-q", "-m", "feature")

        cwd = os.getcwd()
        os.chdir(root)
        try:
            rng = unreleased_range(releases, tag_glob="v*")
        finally:
            os.chdir(cwd)
        assert rng == f"{released}..HEAD", rng


class TestRenameReleasableKeepsTheFormat:

    def test_renaming_carries_the_declared_format_over(self, tmp_path):
        """The renamer reads the format off the releasable, so absence and a
        bare-version declaration both survive the rename."""
        from rlsbl.commands.monorepo import releasable_rename

        root = _bare_version_root_workspace(tmp_path)
        projects = load_workspace(str(root))
        releasables = load_releasables(str(root), projects)
        target = releasables[0]
        assert target.effective_tag_format == STANDALONE_TAG_FORMAT

        source = Path(releasable_rename.__file__).read_text(encoding="utf-8")
        assert ".effective_tag_format" in source
        assert re.search(r"\.tag_format\b(?!\s*\()", source) is None, (
            "the renamer must not read the declared value where it needs a "
            "usable format: an undeclared releasable would rename into a tag "
            "format of None"
        )


class TestLoaderErrorsReachTheCaller:
    """A broken workspace must not resolve to "no releasable config".

    ``resolve_releasable_config_dir`` caught every exception and returned
    ``None``, which reads as "this directory belongs to no releasable" -- a
    legitimate answer for a standalone repo, and a lie for a workspace whose
    loader refused the file. The cost was real: a publish router generated
    with unrendered template variables, and a debugging session spent looking
    for the missing config instead of reading the loader error that was never
    raised.
    """

    def _broken_workspace(self, root):
        """A root-owning releasable with no tag_format -- refused at load."""
        ws_dir = Path(root) / WORKSPACE_DIR
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / WORKSPACE_FILE).write_text(
            '[[releasables]]\nname = "app"\n\n'
            '[[projects]]\npath = "."\nname = "root"\nreleasable = "app"\n',
            encoding="utf-8",
        )
        return Path(root)

    def test_a_loader_error_propagates(self, tmp_path):
        from rlsbl.context import resolve_releasable_config_dir
        from rlsbl.errors import WorkspaceError

        root = self._broken_workspace(tmp_path)
        with pytest.raises(WorkspaceError, match="declares no tag_format"):
            resolve_releasable_config_dir(root, root)

    def test_a_missing_workspace_file_is_still_no_releasable(self, tmp_path):
        """The genuine None case survives: nothing to resolve, no error."""
        from rlsbl.context import resolve_releasable_config_dir

        assert resolve_releasable_config_dir(tmp_path, tmp_path) is None

    def test_a_healthy_workspace_still_resolves(self, tmp_path):
        from rlsbl.context import resolve_releasable_config_dir

        root = _bare_version_root_workspace(tmp_path)
        resolved = resolve_releasable_config_dir(root, root)
        assert resolved is not None
        assert resolved.endswith(os.path.join("releasables", "app"))
