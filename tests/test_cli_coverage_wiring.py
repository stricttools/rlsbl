"""In-process CLI wiring tests for every rlsbl command.

Each test patches the command's dispatch target (at the SOURCE module, since
handlers import their run_cmd/helper at call time), invokes the command through
``rlsbl.app.test([...])``, and asserts BOTH the exit code AND the exact parsed
args/flags that reached the dispatch target. This is the canonical pattern from
tests/test_watch_stop.py::TestStopCliWiring.

These tests double as the source of truth for strictcli's ``cli-test-coverage``
check: recording is construction-time, so ``app.test`` records a coverage
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

    def test_remove_by_id(self):
        result, m = _dispatch(
            ["changelog", "remove", "--id", "01HXYZ", "--no-auto-commit"],
            "rlsbl.commands.changelog_cmd.cmd_remove",
        )
        assert result.exit_code == 0, result.stderr
        f = _flags(m)
        assert f["id"] == "01HXYZ"
        assert f["commits"] is None
        assert f["auto-commit"] is False
        assert f["dry-run"] is False

    def test_remove_by_commits(self):
        result, m = _dispatch(
            ["changelog", "remove", "--commits", "abc123,def456"],
            "rlsbl.commands.changelog_cmd.cmd_remove",
        )
        assert result.exit_code == 0, result.stderr
        f = _flags(m)
        assert f["commits"] == "abc123,def456"
        assert f["id"] is None
        # The opt-out boolean's absence resolves to the fallback its help names.
        assert f["auto-commit"] is True

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

    def test_yank(self):
        result, m = _dispatch(
            ["release", "yank", "0.9.1", "--reason", "broken"],
            "rlsbl.commands.yank.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        args, flags = m.call_args[0][0], m.call_args[0][1]
        assert args == ["0.9.1"]
        assert flags["reason"] == "broken"

    def test_reconcile(self):
        result, m = _dispatch(
            # The documented shape: the reserved flag follows the command.
            # strictcli recognizes the quartet anywhere in argv.
            ["release", "reconcile", "--plan", "--push-timeout", "45",
             "--dry-run"],
            "rlsbl.commands.release_reconcile.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        flags = m.call_args[0][0]
        assert flags["push-timeout"] == 45
        assert flags["dry-run"] is True
        assert flags["quiet"] is False
        assert flags["mode"] == "plan"

    def test_reconcile_apply_elects_the_other_half(self):
        result, m = _dispatch(
            ["release", "reconcile", "--apply"],
            "rlsbl.commands.release_reconcile.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0]["mode"] == "apply"

    def test_reconcile_requires_a_mode(self):
        """Which half of a force-pushing repair you run is never implicit."""
        result, _m = _dispatch(
            ["release", "reconcile"],
            "rlsbl.commands.release_reconcile.run_cmd",
        )
        assert result.exit_code != 0
        assert "--plan" in result.stderr or "mode" in result.stderr

    def test_backfill(self):
        result, m = _dispatch(
            ["release", "backfill", "--overrides", "descriptions.toml",
             "--dry-run"],
            "rlsbl.commands.release_backfill.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        flags = m.call_args[0][0]
        assert flags["overrides"] == "descriptions.toml"
        assert flags["dry-run"] is True
        # The opt-out boolean's fallback: absent means commit.
        assert flags["auto-commit"] is True

    def test_backfill_no_auto_commit(self):
        result, m = _dispatch(
            ["release", "backfill", "--no-auto-commit"],
            "rlsbl.commands.release_backfill.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        flags = m.call_args[0][0]
        assert flags["auto-commit"] is False
        assert flags["overrides"] is None

    def test_reconcile_zero_push_timeout_means_unset(self):
        """0 is the "use the configured default" sentinel, not a real timeout."""
        result, m = _dispatch(
            ["release", "reconcile", "--plan"],
            "rlsbl.commands.release_reconcile.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0]["push-timeout"] is None


# ---------------------------------------------------------------------------
# release run / monorepo release run -- flag surface (build_release_flags)
# ---------------------------------------------------------------------------

class TestReleaseFlagSurface:

    @staticmethod
    def _project_with_release_file(tmp_path, monkeypatch):
        """Chdir into a minimal rlsbl project carrying its own release file.

        The release file is the only way to state a release's intent, so the
        flag surface can only be reached through one. The real dev repo carries
        an in-flight unreleased.toml of its own during active development, so
        these tests run against a project cwd of their own. Coverage still
        records into the App-construction repo regardless of chdir.
        """
        (tmp_path / ".rlsbl" / "releases").mkdir(parents=True)
        (tmp_path / ".rlsbl" / "config.json").write_text(
            '{"publish_mode": "ci", "targets": ["pypi"]}\n'
        )
        (tmp_path / ".rlsbl" / "releases" / "unreleased.toml").write_text(
            'format_version = 1\n'
            'bump = "patch"\n'
            'description = "d"\n'
            'include = ["pypi"]\n'
            'exclude = []\n'
        )
        monkeypatch.chdir(tmp_path)

    def test_release_run_flag_surface(self, tmp_path, monkeypatch):
        self._project_with_release_file(tmp_path, monkeypatch)
        with patch("rlsbl.commands.release.run_cmd") as m:
            result = app.test(
                ["release", "run", "--no-allow-dirty", "--watch"]
            )
        assert result.exit_code == 0, result.stderr
        release_config = m.call_args[0][0]
        assert release_config.bump == "patch"
        assert release_config.description == "d"
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
            "quiet": False,
            "allow-dirty": False,
            "watch": True,
            "push-timeout": None,
            "ci-timeout": None,
            "check-timeout": None,
            "hook-timeout": None,
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
        assert m.call_args[0][2] == {"dry-run": False, "force-publish": False}

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
            # The handler hands this straight to ctx.payload, which validates
            # it against the command's declared schema.
            ret={
                "latest_release": None,
                "latest_release_in_checkout": None,
                "nearest_release_commit_version": None,
                "commits": [],
            },
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
        result, m = _dispatch(
            ["monorepo", "init", "--root-dev-node"],
            "rlsbl.commands.monorepo._cmd_init",
        )
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {"auto-commit": True, "root-dev-node": True}

    def test_init_requires_the_root_members_kind(self):
        result, _m = _dispatch(["monorepo", "init"], "rlsbl.commands.monorepo._cmd_init")
        assert result.exit_code == 1
        assert "--root-dev-node" in result.stderr
        assert "--root-releasable" in result.stderr

    def test_init_root_releasable_carries_its_tag_format(self):
        result, m = _dispatch(
            ["monorepo", "init", "--root-releasable", "core",
             "--tag-format", "v{version}"],
            "rlsbl.commands.monorepo._cmd_init",
        )
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {
            "auto-commit": True,
            "root-releasable": "core",
            "root-tag-format": "v{version}",
        }

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
            ["monorepo", "snapshot"],
            "rlsbl.commands.monorepo._cmd_snapshot",
        )
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {}

    def test_snapshot_check(self):
        """The read-only half is its own command, not a flag on the writer."""
        result, m = _dispatch(
            ["monorepo", "snapshot-check"],
            "rlsbl.commands.monorepo._cmd_snapshot_check",
        )
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {}

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
            ret={"packages": {}, "edges": []},
        )
        assert result.exit_code == 0, result.stderr
        flags = _flags(m)
        assert flags["format"] == "dot"
        assert flags["root"] == "pkgA"
        assert flags["depth"] == 2
        assert flags["json"] is False

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
            ["monorepo", "impact", "--since", "HEAD~2", "--depth", "3"],
            "rlsbl.commands.monorepo._cmd_impact",
            variadic=["pkgA"],
            ret={
                "input": "pkgA", "direct_dependents": [],
                "transitive_dependents": [], "test_scope": [],
                "release_candidates": [],
            },
        )
        assert result.exit_code == 0, result.stderr
        args, flags = m.call_args[0][0], m.call_args[0][1]
        assert args == ["pkgA"]
        assert flags["since"] == "HEAD~2"
        assert flags["depth"] == 3
        assert flags["json"] is False

    def test_impact_has_no_local_format_flag(self):
        """--format died with the json arm: the envelope is the machine form."""
        result, _m = _dispatch(
            ["monorepo", "impact", "--format", "json"],
            "rlsbl.commands.monorepo._cmd_impact",
            variadic=["pkgA"],
        )
        assert result.exit_code == 1
        assert "unknown flag '--format'" in result.stderr

    def test_release_init(self):
        result, m = _dispatch(
            ["monorepo", "release", "init", "--releasables", "a,b"],
            "rlsbl.commands.monorepo._cmd_batch_release_init",
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args.kwargs["releasables"] == "a,b"

    def test_release_init_default_all(self):
        result, m = _dispatch(
            ["monorepo", "release", "init"],
            "rlsbl.commands.monorepo._cmd_batch_release_init",
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args.kwargs["releasables"] is None

    def test_release_init_old_packages_spelling_is_gone(self):
        """The flag selects releasables, and only the new spelling parses."""
        result, _m = _dispatch(
            ["monorepo", "release", "init", "--packages", "a,b"],
            "rlsbl.commands.monorepo._cmd_batch_release_init",
        )
        assert result.exit_code != 0
        assert "unknown flag '--packages'" in result.stderr

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
# decorator = first positional). extract / absorb / rename-releasable all
# stack their two @arg decorators bottom-first so the binding matches the
# documented `<first> <second>` usage in each command's help text. The
# assertions below lock that correct wiring.

_FAKE_WS = "/fake/workspace"


class TestMonorepoWorkspaceGatedWiring:

    _EXTRACT_WS = {
        "rlsbl.commands.monorepo.extract_cmd.find_workspace_root": _FAKE_WS,
    }

    def test_extract(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "extract", "posA", "posB"],
            "rlsbl.commands.monorepo.extract_cmd.cmd_extract",
            extra=self._EXTRACT_WS,
        )
        assert result.exit_code == 0, result.stderr
        # cmd_extract(ws_root, releasable_name, target_path, dry_run=...,
        # delete_with_rm=...). Documented order:
        # `extract <releasable_name> <target_path>`.
        assert m.call_args[0][1] == "posA"  # releasable_name (first token)
        assert m.call_args[0][2] == "posB"  # target_path (second token)
        assert m.call_args.kwargs["dry_run"] is True
        assert m.call_args.kwargs["delete_with_rm"] is False

    def test_extract_delete_with_rm(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "extract", "--delete-with-rm", "posA", "posB"],
            "rlsbl.commands.monorepo.extract_cmd.cmd_extract",
            extra=self._EXTRACT_WS,
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args.kwargs["delete_with_rm"] is True

    _ABSORB_WS = {
        "rlsbl.commands.monorepo.absorb_cmd.find_workspace_root": _FAKE_WS,
    }

    def test_absorb(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "absorb", "posA", "posB"],
            "rlsbl.commands.monorepo.absorb_cmd.cmd_absorb",
            extra=self._ABSORB_WS,
        )
        assert result.exit_code == 0, result.stderr
        # cmd_absorb(ws_root, source_repo, dest_path, name=..., dry_run=...)
        # Documented order: `absorb <source_repo> <dest_path>`.
        assert m.call_args[0][0] == _FAKE_WS
        assert m.call_args[0][1] == "posA"  # source_repo (first token)
        assert m.call_args[0][2] == "posB"  # dest_path (second token)
        assert m.call_args.kwargs["dry_run"] is True
        assert m.call_args.kwargs["delete_with_rm"] is False

    def test_absorb_tag_format_and_deletion_choice(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "absorb", "--tag-format", "{name}@v{version}",
             "--delete-with-rm", "posA", "posB"],
            "rlsbl.commands.monorepo.absorb_cmd.cmd_absorb",
            extra=self._ABSORB_WS,
        )
        assert result.exit_code == 0, result.stderr
        assert m.call_args.kwargs["tag_format"] == "{name}@v{version}"
        assert m.call_args.kwargs["delete_with_rm"] is True

    def test_extract_releasable_is_gone(self):
        """The package-level and releasable-level commands collapsed into one.

        ``monorepo extract`` now takes the releasable name that
        ``extract-releasable`` used to take, so the retired spelling must not
        quietly keep working under a different handler.
        """
        assert "extract-releasable" not in app._groups["monorepo"].commands
        result = app.test(["monorepo", "extract-releasable", "core", "/out"])
        assert result.exit_code != 0

    def test_cleanup(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "cleanup"],
            "rlsbl.releasable_cleanup.run_cleanup_command",
            extra={"rlsbl.workspace.find_workspace_root": _FAKE_WS},
        )
        assert result.exit_code == 0, result.stderr
        # run_cleanup_command(ws_root, dry_run=...)
        assert m.call_args[0][0] == _FAKE_WS
        assert m.call_args.kwargs["dry_run"] is True

    def test_migrate_releasable_is_gone(self):
        """migrate-releasable is deleted: implicit mode is refused at load.

        Nothing in this tree can produce the per-package release state the
        migration consumed, so the command registration is gone too.
        """
        assert "migrate-releasable" not in app._groups["monorepo"].commands
        result = app.test(["monorepo", "migrate-releasable", "myrel"])
        assert result.exit_code != 0

    def test_rename_releasable(self):
        result, m = _dispatch(
            ["--dry-run", "monorepo", "rename-releasable", "oldname", "newname"],
            "rlsbl.commands.monorepo.releasable_rename.rename_releasable",
            ret={"plan": ["step"], "note": None},
            extra={"rlsbl.workspace.find_workspace_root": _FAKE_WS},
        )
        assert result.exit_code == 0, result.stderr
        # rename_releasable(ws_root, old_name, new_name, dry_run=...)
        # Correct binding: first token -> old_name, second -> new_name.
        assert m.call_args[0][0] == _FAKE_WS
        assert m.call_args[0][1] == "oldname"
        assert m.call_args[0][2] == "newname"
        assert m.call_args.kwargs["dry_run"] is True


# ---------------------------------------------------------------------------
# rewrite group
# ---------------------------------------------------------------------------
# Both handlers resolve the project root from cwd before dispatching, and this
# module runs with the repo as cwd (see the module-level repo_cwd mark), so the
# real rlsbl project satisfies that. The dispatch targets are patched at the
# module that defines them, which is what the handler imports, so nothing
# sweeps this repo.


class TestRewriteWiring:

    def test_go_module_path(self):
        result, m = _dispatch(
            ["rewrite", "go-module-path",
             "--from-module", "github.com/o/foo",
             "--to-module", "github.com/n/qux"],
            "rlsbl.commands.rewrite.go_module_path.cmd_go_module_path",
        )
        assert result.exit_code == 0, result.stderr
        f = _flags(m)
        assert f["from-module"] == "github.com/o/foo"
        assert f["to-module"] == "github.com/n/qux"
        assert f["dry-run"] is False

    def test_go_module_path_dry_run(self):
        result, m = _dispatch(
            ["--dry-run", "rewrite", "go-module-path",
             "--from-module", "a/b", "--to-module", "c/d"],
            "rlsbl.commands.rewrite.go_module_path.cmd_go_module_path",
        )
        assert result.exit_code == 0, result.stderr
        assert _flags(m)["dry-run"] is True

    def test_go_module_path_requires_both_modules(self):
        result, _m = _dispatch(
            ["rewrite", "go-module-path", "--from-module", "a/b"],
            "rlsbl.commands.rewrite.go_module_path.cmd_go_module_path",
        )
        assert result.exit_code == 1
        assert "to-module" in result.stderr

    def test_uv_path_sources(self):
        result, m = _dispatch(
            ["rewrite", "uv-path-sources"],
            "rlsbl.commands.rewrite.uv_path_sources.cmd_uv_path_sources",
        )
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {"dry-run": False}

    def test_uv_path_sources_dry_run(self):
        result, m = _dispatch(
            ["--dry-run", "rewrite", "uv-path-sources"],
            "rlsbl.commands.rewrite.uv_path_sources.cmd_uv_path_sources",
        )
        assert result.exit_code == 0, result.stderr
        assert _flags(m) == {"dry-run": True}


class TestTransitionWiring:

    def test_record_a_non_version_tag(self):
        result, m = _dispatch(
            ["transition", "record", "--non-version-tag", "nightly",
             "--reason", "a nightly build marker"],
            "rlsbl.commands.transition_record_cmd.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        f = _flags(m)
        assert f["kind"] == "non-version-tag"
        assert f["subject"] == "nightly"
        assert f["reason"] == "a nightly build marker"
        assert f["dry-run"] is False
        # The opt-out boolean's fallback: absent means commit.
        assert f["auto-commit"] is True

    def test_record_a_closed_release_history(self):
        result, m = _dispatch(
            ["transition", "record", "--release-history-closed", "widget",
             "--reason", "extracted into its own repository",
             "--no-auto-commit", "--dry-run"],
            "rlsbl.commands.transition_record_cmd.run_cmd",
        )
        assert result.exit_code == 0, result.stderr
        f = _flags(m)
        assert f["kind"] == "release-history-closed"
        assert f["subject"] == "widget"
        assert f["auto-commit"] is False
        assert f["dry-run"] is True

    def test_it_requires_a_fact(self):
        """Which fact is being declared is never implicit."""
        result, _m = _dispatch(
            ["transition", "record", "--reason", "why"],
            "rlsbl.commands.transition_record_cmd.run_cmd",
        )
        assert result.exit_code != 0
        assert "--non-version-tag" in result.stderr or "fact" in result.stderr

    def test_the_two_facts_are_mutually_exclusive(self):
        result, _m = _dispatch(
            ["transition", "record", "--non-version-tag", "nightly",
             "--release-history-closed", "widget", "--reason", "why"],
            "rlsbl.commands.transition_record_cmd.run_cmd",
        )
        assert result.exit_code != 0

    def test_it_requires_a_reason(self):
        result, _m = _dispatch(
            ["transition", "record", "--non-version-tag", "nightly"],
            "rlsbl.commands.transition_record_cmd.run_cmd",
        )
        assert result.exit_code != 0
        assert "reason" in result.stderr


# ---------------------------------------------------------------------------
# The committed coverage manifest
# ---------------------------------------------------------------------------


class TestTheCommittedCoverageManifest:
    """``.strictcli/test-coverage.json`` is committed, and it goes stale silently.

    The framework's check unions the manifest with the local shard files and
    never subtracts, so a command that is REMOVED stays recorded as covered
    forever -- the manifest is monotonic by design, and only a deliberate
    regeneration prunes it. This test is that deliberateness: an entry naming a
    command the app no longer registers is a stale manifest, not coverage.
    """

    def test_it_names_only_commands_the_app_still_registers(self):
        import json
        import pathlib

        manifest_path = (
            pathlib.Path(__file__).resolve().parents[1]
            / ".strictcli" / "test-coverage.json"
        )
        recorded = set(json.loads(manifest_path.read_text(encoding="utf-8")))
        live = rlsbl.app._collect_all_command_paths()
        assert sorted(recorded - live) == [], (
            "stale entries in .strictcli/test-coverage.json: these commands no "
            "longer exist. Delete the manifest and the local shards under "
            ".strictcli/coverage/, re-run the suite, and re-run "
            "`rlsbl check --name cli-test-coverage` to rewrite it."
        )
