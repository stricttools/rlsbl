"""In-process CLI wiring tests for every rlsbl command.

Each test patches the command's dispatch target (at the SOURCE module, since
handlers import their run_cmd/helper at call time), invokes the command through
``rlsbl.app.test([...])``, and asserts BOTH the exit code AND the exact parsed
args/flags that reached the dispatch target. This is the canonical pattern from
tests/test_watch_stop.py::TestStopCliWiring.

These tests double as the source of truth for strictcli's ``cli-test-coverage``
check: recording is construction-anchored, so ``app.test`` records a coverage
hit into the repo where the App object was constructed (the repo root, under
pytest) regardless of any chdir. A coverage hit is recorded on a SUCCESSFUL
PARSE, before the handler body runs -- so patching the dispatch target both
records coverage and prevents the real (potentially mutating) command from
executing against this repo.

Safety: every dispatch target is patched BEFORE app.test() runs. Pre-dispatch
handler bodies for all covered commands are read-only (project-root checks,
context creation, flag dict building) -- none write to disk or shell out before
the deferred import/call, so no tmp-cwd isolation is required.
"""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

import rlsbl

# These tests dispatch real CLI commands through ``app.test()`` and rely on the
# process cwd being the real rlsbl project: the pre-dispatch handler bodies
# resolve the project root from cwd, and strictcli coverage is recorded into the
# App-construction repo. They are the documented exception to the autouse
# tmp-cwd isolation in conftest, so the whole module opts out.
pytestmark = pytest.mark.repo_cwd

app = rlsbl.app


def _dispatch(argv, target, *, variadic=None, ret=None, extra=None):
    """Patch ``target`` (and any ``extra`` targets), run app.test(argv).

    Returns ``(result, mock)`` where ``mock`` is the patched dispatch target.
    ``variadic`` patches ``rlsbl._variadic_args`` (for commands that read
    positional args out of band). ``ret`` sets the target's return_value.
    ``extra`` is a dict of ``{dotted_path: return_value}`` for additional
    patches (e.g. workspace resolution) needed to reach the target.
    """
    with ExitStack() as stack:
        mock = stack.enter_context(patch(target))
        if ret is not None:
            mock.return_value = ret
        if variadic is not None:
            stack.enter_context(patch("rlsbl._variadic_args", variadic))
        for dotted, rv in (extra or {}).items():
            em = stack.enter_context(patch(dotted))
            em.return_value = rv
        result = app.test(argv)
    return result, mock


def _flags(mock):
    """First positional arg of the dispatch call (the flags dict for the
    ``cmd_*(flags, project_root=...)`` handler family)."""
    return mock.call_args[0][0]


# ---------------------------------------------------------------------------
# changelog group
# ---------------------------------------------------------------------------

class TestChangelogWiring:

    def test_amend(self):
        result, m = _dispatch(
            ["changelog", "amend", "--version", "0.1.0", "--commits", "abc123",
             "--description", "Fixed X", "--type", "fix"],
            "rlsbl.commands.changelog_cmd.cmd_amend",
        )
        assert result.exit_code == 0, result.stderr
        f = _flags(m)
        assert f["version"] == "0.1.0"
        assert f["commits"] == "abc123"
        assert f["description"] == "Fixed X"
        assert f["type"] == "fix"
        assert f["user-facing"] is True
        assert f["validate-hashes"] is True

    def test_edit(self):
        result, m = _dispatch(
            ["changelog", "edit", "--commits", "def456", "--type", "feature",
             "--description", "New thing", "--no-user-facing"],
            "rlsbl.commands.changelog_cmd.cmd_edit",
        )
        assert result.exit_code == 0, result.stderr
        f = _flags(m)
        assert f["commits"] == "def456"
        assert f["type"] == "feature"
        assert f["description"] == "New thing"
        assert f["user-facing"] is False

    def test_generate(self):
        result, m = _dispatch(
            ["changelog", "generate", "--no-auto-commit"],
            "rlsbl.commands.changelog_cmd.cmd_generate",
        )
        assert result.exit_code == 0, result.stderr
        f = _flags(m)
        assert f["auto-commit"] is False
        assert f["dry-run"] is False

    def test_remap(self):
        result, m = _dispatch(
            ["changelog", "remap", "--map-file", "map.txt"],
            "rlsbl.commands.changelog_cmd.cmd_remap",
        )
        assert result.exit_code == 0, result.stderr
        f = _flags(m)
        assert f["map-file"] == "map.txt"
        assert f["from-journal"] is False
        assert f["stdin"] is False


# ---------------------------------------------------------------------------
# release group (deprecate, edit, undo, yank)
# ---------------------------------------------------------------------------

class TestReleaseGroupWiring:

    def test_deprecate(self):
        result, m = _dispatch(
            ["release", "deprecate", "0.9.1", "--reason", "insecure", "--use", "0.9.2"],
            "rlsbl.commands.deprecate.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        args, flags = m.call_args[0][0], m.call_args[0][1]
        assert args == ["0.9.1"]
        assert flags["reason"] == "insecure"
        assert flags["use"] == "0.9.2"

    def test_edit(self):
        result, m = _dispatch(
            ["release", "edit", "1.2.3"],
            "rlsbl.commands.edit_release.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        args, flags = m.call_args[0][0], m.call_args[0][1]
        assert args == ["1.2.3"]
        assert flags["dry-run"] is False

    def test_edit_defaults_to_current(self):
        result, m = _dispatch(
            ["release", "edit"],
            "rlsbl.commands.edit_release.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0] == []  # no version -> empty args

    def test_undo(self):
        result, m = _dispatch(
            ["release", "undo", "--version", "0.1.0"],
            "rlsbl.commands.undo.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        target_arg = m.call_args[0][0]
        flags = m.call_args[0][2]
        assert target_arg is None  # no --target -> None
        assert flags["version"] == "0.1.0"
        assert flags["yes"] is False

    def test_yank(self):
        result, m = _dispatch(
            ["release", "yank", "0.9.1", "--reason", "broken"],
            "rlsbl.commands.yank.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        args, flags = m.call_args[0][0], m.call_args[0][1]
        assert args == ["0.9.1"]
        assert flags["reason"] == "broken"


# ---------------------------------------------------------------------------
# release run / monorepo release run -- flag surface (build_release_flags)
# ---------------------------------------------------------------------------

class TestReleaseFlagSurface:

    class _Target:
        def __init__(self, name):
            self.name = name

    @staticmethod
    def _clean_project(tmp_path, monkeypatch):
        """Chdir into a minimal rlsbl project with NO release file.

        The quick-bump path refuses when a release file exists; the real dev
        repo carries an in-flight unreleased.toml during active development, so
        these flag-surface tests must run against a clean project cwd. Coverage
        still records into the App-construction repo regardless of chdir.
        """
        (tmp_path / ".rlsbl").mkdir()
        (tmp_path / ".rlsbl" / "config.json").write_text(
            '{"publish_mode": "ci", "targets": ["pypi"]}\n'
        )
        monkeypatch.chdir(tmp_path)

    def test_release_run_quick_bump_flag_surface(self, tmp_path, monkeypatch):
        # --bump quick mode bypasses the release file. Patch detect_targets so
        # the flag surface is exercised deterministically regardless of what
        # this repo detects.
        self._clean_project(tmp_path, monkeypatch)
        with patch("rlsbl.commands.release.run_cmd") as m, \
             patch("rlsbl.targets.detect_targets", return_value=[self._Target("pypi")]):
            result = app.test(
                ["release", "run", "--bump", "patch", "--description", "d",
                 "--no-allow-dirty", "--watch"]
            )
        assert result.exit_code == 0, result.stderr
        flags = m.call_args[0][1]
        assert flags["allow-dirty"] is False
        assert flags["watch"] is True

    def test_monorepo_release_run_flag_surface(self):
        result, m = _dispatch(
            ["monorepo", "release", "run", "--no-allow-dirty", "--watch"],
            "rlsbl.commands.monorepo._cmd_batch_release",
        )
        assert result.exit_code == 0, result.stderr
        flags = _flags(m)
        assert flags == {
            "dry-run": False,
            "yes": False,
            "quiet": False,
            "allow-dirty": False,
            "watch": True,
        }

    def test_monorepo_release_run_no_watch(self):
        result, m = _dispatch(
            ["monorepo", "release", "run", "--allow-dirty", "--no-watch"],
            "rlsbl.commands.monorepo._cmd_batch_release",
        )
        assert result.exit_code == 0, result.stderr
        flags = _flags(m)
        assert flags["allow-dirty"] is True
        assert flags["watch"] is False


# ---------------------------------------------------------------------------
# standalone commands
# ---------------------------------------------------------------------------

class TestStandaloneWiring:

    def test_claim_name(self):
        result, m = _dispatch(
            ["claim-name", "--target", "npm"],
            "rlsbl.commands.claim_name.run_cmd",
            variadic=["mypkg"],
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0] == "npm"
        assert m.call_args[0][1] == ["mypkg"]
        assert m.call_args[0][2] == {"yes": False, "dry-run": False}

    def test_commit(self):
        result, m = _dispatch(
            ["commit", "-m", "generated"],
            "rlsbl.commands.commit_cmd.run_cmd",
            variadic=["out.json"],
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0] == "generated"
        assert m.call_args[0][1] == ["out.json"]

    def test_deploy(self):
        result, m = _dispatch(
            ["deploy", "prod", "--target", "npm"],
            "rlsbl.commands.deploy_cmd.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0] == "npm"
        assert m.call_args[0][1] == ["prod"]
        assert m.call_args[0][2] == {"dry-run": False}

    def test_prs(self):
        result, m = _dispatch(["prs"], "rlsbl.commands.prs.run_cmd")
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0] is None
        assert m.call_args[0][1] == []
        assert m.call_args[0][2] == {}

    def test_targets(self):
        result, m = _dispatch(["targets"], "rlsbl.commands.targets_cmd.run_cmd")
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0] is None
        assert m.call_args[0][1] == []
        assert m.call_args[0][2] == {}

    def test_unreleased(self):
        result, m = _dispatch(
            ["unreleased", "--json"],
            "rlsbl.commands.unreleased.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][2] == {"json": True}

    def test_dev_status(self):
        result, m = _dispatch(
            ["dev", "status"],
            "rlsbl.commands.dev_sync.run_status",
            ret=0,
        )
        assert result.exit_code == 0, result.stderr
        # run_status(root) -- root is the resolved project Path
        assert m.call_args[0][0] is not None

    def test_pre_push_check_is_retired(self):
        # No dispatch target: the handler prints a removal notice and exits 1.
        # Coverage is still recorded because the parse succeeds.
        result = app.test(["pre-push-check"])
        assert result.exit_code == 1
        assert "removed" in result.stderr


# ---------------------------------------------------------------------------
# monorepo commands reachable directly from a standalone project root
# ---------------------------------------------------------------------------

class TestMonorepoDirectWiring:

    def test_init(self):
        result, m = _dispatch(["monorepo", "init"], "rlsbl.commands.monorepo._cmd_init")
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {"auto-commit": True}

    def test_add(self):
        result, m = _dispatch(
            ["monorepo", "add", "pkgs/foo", "--target", "npm", "--name", "Foo",
             "--library", "true"],
            "rlsbl.commands.monorepo._cmd_add",
        )
        assert result.exit_code == 0, result.stderr
        args, flags = m.call_args[0][0], m.call_args[0][1]
        assert args == ["pkgs/foo"]
        assert flags["target"] == "npm"
        assert flags["name"] == "Foo"
        assert flags["library"] == "true"

    def test_remove(self):
        result, m = _dispatch(
            ["monorepo", "remove", "pkgs/foo"],
            "rlsbl.commands.monorepo._cmd_remove",
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0] == ["pkgs/foo"]
        assert m.call_args[0][1] == {}

    def test_list(self):
        result, m = _dispatch(["monorepo", "list"], "rlsbl.commands.monorepo._cmd_list")
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {}

    def test_sync(self):
        result, m = _dispatch(["monorepo", "sync"], "rlsbl.commands.monorepo._cmd_sync")
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {"auto-commit": True}

    def test_status(self):
        result, m = _dispatch(["monorepo", "status"], "rlsbl.commands.monorepo._cmd_status")
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {}

    def test_outdated(self):
        result, m = _dispatch(["monorepo", "outdated"], "rlsbl.commands.monorepo._cmd_outdated")
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {}

    def test_snapshot(self):
        result, m = _dispatch(
            ["monorepo", "snapshot", "--check"],
            "rlsbl.commands.monorepo._cmd_snapshot",
        )
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {"check": True}

    def test_mirror(self):
        result, m = _dispatch(
            ["monorepo", "mirror", "myproj"],
            "rlsbl.commands.monorepo._cmd_mirror",
        )
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {"project": "myproj", "dry-run": False}

    def test_graph(self):
        result, m = _dispatch(
            ["monorepo", "graph", "--format", "dot", "--root", "pkgA", "--depth", "2"],
            "rlsbl.commands.monorepo._cmd_graph",
        )
        assert result.exit_code == 0, result.stderr
        flags = _flags(m)
        assert flags["format"] == "dot"
        assert flags["root"] == "pkgA"
        assert flags["depth"] == 2

    def test_check_names(self):
        result, m = _dispatch(
            ["monorepo", "check-names", "--target", "npm", "--prefix", "p-",
             "--suffix", "-js"],
            "rlsbl.commands.monorepo._cmd_check_names",
            variadic=["pkgA"],
        )
        assert result.exit_code == 0, result.stderr
        names, flags = m.call_args[0][0], m.call_args[0][1]
        assert names == ["pkgA"]
        assert flags == {"target": "npm", "prefix": "p-", "suffix": "-js", "delay": "200"}

    def test_impact(self):
        result, m = _dispatch(
            ["monorepo", "impact", "--format", "json", "--since", "HEAD~2", "--depth", "3"],
            "rlsbl.commands.monorepo._cmd_impact",
            variadic=["pkgA"],
        )
        assert result.exit_code == 0, result.stderr
        args, flags = m.call_args[0][0], m.call_args[0][1]
        assert args == ["pkgA"]
        assert flags["format"] == "json"
        assert flags["since"] == "HEAD~2"
        assert flags["depth"] == 3

    def test_release_init(self):
        result, m = _dispatch(
            ["monorepo", "release", "init", "--packages", "a,b"],
            "rlsbl.commands.monorepo._cmd_batch_release_init",
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args.kwargs["packages"] == "a,b"

    def test_release_init_default_all(self):
        result, m = _dispatch(
            ["monorepo", "release", "init"],
            "rlsbl.commands.monorepo._cmd_batch_release_init",
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args.kwargs["packages"] is None

    def test_release_order(self):
        result, m = _dispatch(
            ["monorepo", "release", "order"],
            "rlsbl.commands.monorepo._cmd_release_order",
        )
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {}


# ---------------------------------------------------------------------------
# monorepo commands gated on a workspace root
# ---------------------------------------------------------------------------
# These handlers require a workspace (find_workspace_root != None) before
# reaching the dispatch target. This repo is a standalone project, so
# find_workspace_root is patched to a sentinel path to drive execution into
# the target. Each target's return_value is a realistic dict because the
# handler formats it into a status line after the (patched) call returns.
#
# strictcli binds positionals in @arg-decorator registration order (bottom
# decorator = first positional). extract / absorb / extract-releasable /
# rename-releasable all stack their two @arg decorators bottom-first so the
# binding matches the documented `<first> <second>` usage in each command's
# help text. The assertions below lock that correct wiring.

_FAKE_WS = "/fake/workspace"


class TestMonorepoWorkspaceGatedWiring:

    def test_extract(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "extract", "posA", "posB"],
            "rlsbl.commands.monorepo.cmd_extract",
            ret={"package_name": "p", "package_path": "pp", "target_path": "tp"},
            extra={"rlsbl.workspace.find_workspace_root": _FAKE_WS},
        )
        assert result.exit_code == 0, result.stderr
        # cmd_extract(ws_root, package_name, target_path, dry_run=...)
        # Documented order: `extract <package_name> <target_path>`.
        assert m.call_args[0][0] == _FAKE_WS
        assert m.call_args[0][1] == "posA"  # package_name (first token)
        assert m.call_args[0][2] == "posB"  # target_path (second token)
        assert m.call_args.kwargs["dry_run"] is True

    def test_absorb(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "absorb", "posA", "posB"],
            "rlsbl.commands.monorepo.cmd_absorb",
            ret={"name": "p", "source_path": "s", "dest_path": "posB",
                 "tags_to_import": []},
            extra={"rlsbl.workspace.find_workspace_root": _FAKE_WS},
        )
        assert result.exit_code == 0, result.stderr
        # cmd_absorb(ws_root, source_repo, dest_path, name=..., dry_run=...)
        # Documented order: `absorb <source_repo> <dest_path>`.
        assert m.call_args[0][0] == _FAKE_WS
        assert m.call_args[0][1] == "posA"  # source_repo (first token)
        assert m.call_args[0][2] == "posB"  # dest_path (second token)
        assert m.call_args.kwargs["dry_run"] is True

    def test_extract_releasable(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "extract-releasable", "posA", "posB"],
            "rlsbl.commands.monorepo.cmd_extract_releasable",
            ret={"member_packages": ["m"], "is_monorepo": False,
                 "releasable_name": "r", "target_path": "t"},
            extra={"rlsbl.workspace.find_workspace_root": _FAKE_WS},
        )
        assert result.exit_code == 0, result.stderr
        # cmd_extract_releasable(ws_root, releasable_name, target_path, dry_run=...)
        # Documented order: `extract-releasable <releasable_name> <target_path>`.
        assert m.call_args[0][0] == _FAKE_WS
        assert m.call_args[0][1] == "posA"  # releasable_name (first token)
        assert m.call_args[0][2] == "posB"  # target_path (second token)
        assert m.call_args.kwargs["dry_run"] is True

    def test_cleanup(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "cleanup"],
            "rlsbl.releasable_cleanup.run_cleanup_command",
            extra={
                "rlsbl.workspace.find_workspace_root": _FAKE_WS,
                "rlsbl.workspace.is_explicit_mode": True,
            },
        )
        assert result.exit_code == 0, result.stderr
        # run_cleanup_command(ws_root, dry_run=..., yes=...)
        assert m.call_args[0][0] == _FAKE_WS
        assert m.call_args.kwargs["dry_run"] is True
        assert m.call_args.kwargs["yes"] is False

    def test_migrate_releasable(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "migrate-releasable", "myrel"],
            "rlsbl.releasable_migration.cmd_migrate_releasable",
            ret={"members": ["m"], "tag_format": "v{version}", "state": {"projects": []}},
            extra={"rlsbl.workspace.find_workspace_root": _FAKE_WS},
        )
        assert result.exit_code == 0, result.stderr
        # cmd_migrate_releasable(ws_root, releasable_name, dry_run=..., yes=...)
        assert m.call_args[0][0] == _FAKE_WS
        assert m.call_args[0][1] == "myrel"
        assert m.call_args.kwargs["dry_run"] is True

    def test_rename_releasable(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "rename-releasable", "oldname", "newname"],
            "rlsbl.commands.monorepo.releasable_rename.rename_releasable",
            ret={"plan": ["step"], "note": None},
            extra={"rlsbl.workspace.find_workspace_root": _FAKE_WS},
        )
        assert result.exit_code == 0, result.stderr
        # rename_releasable(ws_root, old_name, new_name, dry_run=..., yes=...)
        # Correct binding: first token -> old_name, second -> new_name.
        assert m.call_args[0][0] == _FAKE_WS
        assert m.call_args[0][1] == "oldname"
        assert m.call_args[0][2] == "newname"
        assert m.call_args.kwargs["dry_run"] is True
