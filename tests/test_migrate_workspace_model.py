"""Tests for the workspace-model migration script.

The script applies the mechanical half of the ownership-model conversion to a
workspace.toml that predates it: it declares the root member (in the kind the
operator names), deletes the retired ``watch`` keys, and moves a member's
mirror destination onto its releasable. Everything it cannot decide -- which
member owns the repository root when two claim it, which releasable a mirrored
member should belong to, which tag scheme a root releasable's history already
uses -- is refused or reported, never guessed.

The old-model fixtures here are RAW TOML strings on purpose: the shared
workspace helpers build the NEW model and would refuse to write the shapes this
script exists to read.
"""

import importlib.util
import io
import sys
import tomllib
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "migrate_workspace_model.py"
)
_spec = importlib.util.spec_from_file_location("migrate_workspace_model", _SCRIPT)
migrate_mod = importlib.util.module_from_spec(_spec)
# Registered before execution: the module defines dataclasses, and @dataclass
# resolves annotations through sys.modules[cls.__module__].
sys.modules["migrate_workspace_model"] = migrate_mod
_spec.loader.exec_module(migrate_mod)

MigrationError = migrate_mod.MigrationError


# ---------------------------------------------------------------------------
# Fixture builders -- raw old-model documents
# ---------------------------------------------------------------------------


def write_workspace(repo, text):
    """Write a raw workspace.toml, byte for byte as given."""
    ws_dir = Path(repo) / ".rlsbl-monorepo"
    ws_dir.mkdir(parents=True, exist_ok=True)
    path = ws_dir / "workspace.toml"
    path.write_text(text, encoding="utf-8")
    return path


def workspace_path(repo):
    return Path(repo) / ".rlsbl-monorepo" / "workspace.toml"


def read_workspace(repo):
    with open(workspace_path(repo), "rb") as f:
        return tomllib.load(f)


# A workspace in the old model: no root member, a watch key on every member,
# and the mirror destination on the member rather than on its releasable.
OLD_MODEL = """\
# The workspace, as an operator wrote it.
[[releasables]]
name = "core"
tag_format = "{name}@v{version}"

[[releasables]]
name = "cli"
tag_format = "{name}@v{version}"

[[projects]]
path = "pkgs/core"
name = "core"
watch = ["pkgs/core/**"]
releasable = "core"

[[projects]]
path = "pkgs/cli"
name = "cli"
watch = ["pkgs/cli/**", "shared/**"]
releasable = "cli"
subtree_remote = "git@github.com:owner/cli.git"
"""

# The same workspace with neither watch keys nor a member-level mirror: only
# the root member is missing.
NO_ROOT_MEMBER = """\
[[releasables]]
name = "core"
tag_format = "{name}@v{version}"

[[projects]]
path = "pkgs/core"
name = "core"
releasable = "core"
"""

IMPLICIT_MODE = """\
[[projects]]
path = "pkgs/core"
name = "core"
watch = ["pkgs/core/**"]
"""


def migrate(
    repo,
    *,
    dev_node=False,
    releasable=None,
    tag_format=None,
    dry_run=False,
    auto_commit=False,
    backfill=False,
):
    """Run the migration against *repo*, returning ``(exit_code, output)``."""
    out = io.StringIO()
    code = migrate_mod.run(
        str(repo),
        root_kind=migrate_mod.RootKind(
            dev_node=dev_node, releasable=releasable, tag_format=tag_format
        ),
        dry_run=dry_run,
        auto_commit=auto_commit,
        backfill=backfill,
        use_gh=False,
        out=out,
    )
    return code, out.getvalue()


def plan_for(repo, *, dev_node=False, releasable=None, tag_format=None):
    return migrate_mod.build_plan(
        str(repo),
        migrate_mod.RootKind(
            dev_node=dev_node, releasable=releasable, tag_format=tag_format
        ),
    )


def root_member(data):
    return next(p for p in data["projects"] if p["path"] == ".")


# ---------------------------------------------------------------------------
# Implicit mode is not migrated
# ---------------------------------------------------------------------------


class TestImplicitModeRefusal:
    """A workspace with no [[releasables]] at all takes the deferred path."""

    @pytest.fixture
    def repo(self, tmp_path):
        write_workspace(tmp_path, IMPLICIT_MODE)
        return tmp_path

    def test_planning_refuses(self, repo):
        with pytest.raises(MigrationError) as exc:
            plan_for(repo, dev_node=True)
        message = str(exc.value)
        assert "implicit" in message
        assert migrate_mod.LAST_IMPLICIT_MODE_VERSION in message
        assert "todo" in message

    def test_the_file_is_untouched(self, repo):
        before = workspace_path(repo).read_bytes()
        code, output = migrate(repo, dev_node=True)
        assert code == 2
        assert workspace_path(repo).read_bytes() == before
        assert "implicit" in output

    def test_an_empty_releasables_section_is_explicit_mode(self, tmp_path):
        write_workspace(
            tmp_path,
            'releasables = []\n\n[[projects]]\npath = "pkgs/core"\nname = "core"\n'
            'releasable = false\n',
        )
        code, _output = migrate(tmp_path, dev_node=True)
        assert code == 0
        assert root_member(read_workspace(tmp_path))["name"] == "root"


# ---------------------------------------------------------------------------
# The root member
# ---------------------------------------------------------------------------


class TestRootMemberDevNode:
    @pytest.fixture
    def repo(self, tmp_path):
        write_workspace(tmp_path, NO_ROOT_MEMBER)
        return tmp_path

    def test_the_member_is_added_with_the_reserved_name(self, repo):
        migrate(repo, dev_node=True)
        member = root_member(read_workspace(repo))
        assert member["name"] == "root"
        assert member["dev_only"] is True
        assert member["releasable"] is False

    def test_the_declared_members_survive(self, repo):
        migrate(repo, dev_node=True)
        data = read_workspace(repo)
        assert [p["path"] for p in data["projects"]] == ["pkgs/core", "."]

    def test_the_plan_names_the_edit(self, repo):
        _code, output = migrate(repo, dev_node=True)
        assert "add the root member" in output
        assert "dev node" in output

    def test_no_releasable_is_invented(self, repo):
        migrate(repo, dev_node=True)
        assert [r["name"] for r in read_workspace(repo)["releasables"]] == ["core"]


class TestRootMemberReleasable:
    @pytest.fixture
    def repo(self, tmp_path):
        write_workspace(tmp_path, NO_ROOT_MEMBER)
        return tmp_path

    def test_the_member_belongs_to_the_named_releasable(self, repo):
        migrate(repo, releasable="monorepo", tag_format="v{version}")
        member = root_member(read_workspace(repo))
        assert member["name"] == "root"
        assert member["releasable"] == "monorepo"
        assert "dev_only" not in member

    def test_the_releasable_is_created_with_the_declared_tag_format(self, repo):
        migrate(repo, releasable="monorepo", tag_format="v{version}")
        rels = {r["name"]: r for r in read_workspace(repo)["releasables"]}
        assert rels["monorepo"]["tag_format"] == "v{version}"

    def test_an_existing_releasable_gains_only_the_tag_format(self, tmp_path):
        write_workspace(
            tmp_path,
            '[[releasables]]\nname = "monorepo"\n\n'
            '[[projects]]\npath = "pkgs/core"\nname = "core"\n'
            'releasable = "monorepo"\n',
        )
        migrate(tmp_path, releasable="monorepo", tag_format="v{version}")
        rels = read_workspace(tmp_path)["releasables"]
        assert len(rels) == 1
        assert rels[0]["tag_format"] == "v{version}"

    def test_a_contradicting_tag_format_is_the_operators_decision(self, tmp_path):
        write_workspace(
            tmp_path,
            '[[releasables]]\nname = "monorepo"\ntag_format = "{name}@v{version}"\n\n'
            '[[projects]]\npath = "pkgs/core"\nname = "core"\n'
            'releasable = "monorepo"\n',
        )
        with pytest.raises(MigrationError) as exc:
            plan_for(tmp_path, releasable="monorepo", tag_format="v{version}")
        assert "{name}@v{version}" in str(exc.value)
        assert "v{version}" in str(exc.value)

    def test_a_tag_format_is_required(self, tmp_path):
        with pytest.raises(MigrationError) as exc:
            migrate_mod.RootKind(dev_node=False, releasable="monorepo")
        assert "tag_format" in str(exc.value) or "tag-format" in str(exc.value)

    def test_a_kind_is_required(self, tmp_path):
        with pytest.raises(MigrationError):
            migrate_mod.RootKind(dev_node=False)


class TestRootMemberAlreadyPresent:
    """A root member that is already declared is completed, never re-guessed."""

    def test_a_matching_kind_is_left_alone(self, tmp_path):
        write_workspace(
            tmp_path,
            'releasables = []\n\n[[projects]]\npath = "."\nname = "root"\n'
            "dev_only = true\nreleasable = false\n",
        )
        before = workspace_path(tmp_path).read_bytes()
        code, output = migrate(tmp_path, dev_node=True)
        assert code == 0
        assert workspace_path(tmp_path).read_bytes() == before
        assert "nothing to migrate" in output.lower()

    def test_a_misnamed_root_member_is_renamed(self, tmp_path):
        write_workspace(
            tmp_path,
            'releasables = []\n\n[[projects]]\npath = "."\nname = "monorepo"\n'
            "dev_only = true\nreleasable = false\n",
        )
        _code, output = migrate(tmp_path, dev_node=True)
        assert root_member(read_workspace(tmp_path))["name"] == "root"
        assert "root" in output

    def test_a_partial_root_member_gains_its_kind_keys(self, tmp_path):
        write_workspace(
            tmp_path, 'releasables = []\n\n[[projects]]\npath = "."\nname = "root"\n'
        )
        migrate(tmp_path, dev_node=True)
        member = root_member(read_workspace(tmp_path))
        assert member["dev_only"] is True
        assert member["releasable"] is False

    def test_a_contradicting_kind_is_refused(self, tmp_path):
        write_workspace(
            tmp_path,
            '[[releasables]]\nname = "monorepo"\ntag_format = "v{version}"\n\n'
            '[[projects]]\npath = "."\nname = "root"\nreleasable = "monorepo"\n',
        )
        with pytest.raises(MigrationError) as exc:
            plan_for(tmp_path, dev_node=True)
        assert "monorepo" in str(exc.value)
        assert "--root-dev-node" in str(exc.value)

    def test_a_second_root_member_is_the_operators_decision(self, tmp_path):
        """Two members claiming the root is residue: the loader says so."""
        write_workspace(
            tmp_path,
            "releasables = []\n\n"
            '[[projects]]\npath = "."\nname = "root"\ndev_only = true\n'
            "releasable = false\n\n"
            '[[projects]]\npath = "./"\nname = "other"\nreleasable = false\n',
        )
        code, output = migrate(tmp_path, dev_node=True)
        assert code == 1
        assert "OPERATOR INPUT REQUIRED" in output
        assert "both declare the repository root" in output

    def test_the_root_path_spelling_is_recognized(self, tmp_path):
        """'./' is the root, so no second root member is invented for it."""
        write_workspace(
            tmp_path,
            'releasables = []\n\n[[projects]]\npath = "./"\nname = "root"\n'
            "dev_only = true\nreleasable = false\n",
        )
        code, output = migrate(tmp_path, dev_node=True)
        assert code == 0
        data = read_workspace(tmp_path)
        assert len(data["projects"]) == 1
        # ...and the spelling is canonicalized, so the file reads like the model.
        assert data["projects"][0]["path"] == "."
        assert "canonical" in output


# ---------------------------------------------------------------------------
# The watch key
# ---------------------------------------------------------------------------


class TestWatchKeys:
    @pytest.fixture
    def repo(self, tmp_path):
        write_workspace(tmp_path, OLD_MODEL)
        return tmp_path

    def test_every_watch_key_is_deleted(self, repo):
        migrate(repo, dev_node=True)
        for member in read_workspace(repo)["projects"]:
            assert "watch" not in member

    def test_the_plan_names_each_one(self, repo):
        _code, output = migrate(repo, dev_node=True)
        assert output.count("delete the watch key") == 2
        assert "projects[0] ('core')" in output
        assert "projects[1] ('cli')" in output

    def test_the_surrounding_declaration_survives(self, repo):
        migrate(repo, dev_node=True)
        text = workspace_path(repo).read_text()
        assert "# The workspace, as an operator wrote it." in text
        data = read_workspace(repo)
        core = next(p for p in data["projects"] if p["name"] == "core")
        assert core["path"] == "pkgs/core"
        assert core["releasable"] == "core"


# ---------------------------------------------------------------------------
# The mirror destination
# ---------------------------------------------------------------------------


MIRRORED_MEMBER = """\
[[releasables]]
name = "cli"
tag_format = "{name}@v{version}"

[[projects]]
path = "."
name = "root"
dev_only = true
releasable = false

[[projects]]
path = "pkgs/cli"
name = "cli"
releasable = "cli"
subtree_remote = "git@github.com:owner/cli.git"
"""


class TestMirrorRelocation:
    @pytest.fixture
    def repo(self, tmp_path):
        write_workspace(tmp_path, MIRRORED_MEMBER)
        return tmp_path

    def test_the_key_moves_onto_the_releasable(self, repo):
        code, output = migrate(repo, dev_node=True)
        assert code == 0
        data = read_workspace(repo)
        member = next(p for p in data["projects"] if p["name"] == "cli")
        assert "subtree_remote" not in member
        assert data["releasables"][0]["subtree_remote"] == (
            "git@github.com:owner/cli.git"
        )
        assert "move the mirror destination" in output

    def test_a_releasable_already_declaring_it_just_loses_the_member_key(
        self, tmp_path
    ):
        write_workspace(
            tmp_path,
            MIRRORED_MEMBER.replace(
                'tag_format = "{name}@v{version}"',
                'tag_format = "{name}@v{version}"\n'
                'subtree_remote = "git@github.com:owner/cli.git"',
            ),
        )
        code, _output = migrate(tmp_path, dev_node=True)
        assert code == 0
        data = read_workspace(tmp_path)
        member = next(p for p in data["projects"] if p["name"] == "cli")
        assert "subtree_remote" not in member
        assert data["releasables"][0]["subtree_remote"] == (
            "git@github.com:owner/cli.git"
        )

    def test_two_different_destinations_are_the_operators_decision(self, tmp_path):
        write_workspace(
            tmp_path,
            MIRRORED_MEMBER.replace(
                'tag_format = "{name}@v{version}"',
                'tag_format = "{name}@v{version}"\n'
                'subtree_remote = "git@github.com:owner/other.git"',
            ),
        )
        with pytest.raises(MigrationError) as exc:
            plan_for(tmp_path, dev_node=True)
        assert "owner/other.git" in str(exc.value)
        assert "owner/cli.git" in str(exc.value)

    def test_a_member_outside_every_releasable_is_refused(self, tmp_path):
        write_workspace(
            tmp_path,
            MIRRORED_MEMBER.replace('releasable = "cli"\nsubtree_remote', "subtree_remote"),
        )
        with pytest.raises(MigrationError) as exc:
            plan_for(tmp_path, dev_node=True)
        message = str(exc.value)
        assert "'cli'" in message
        # Both options are named, and neither is taken for the operator.
        assert "releasable" in message
        assert "delete" in message.lower()

    def test_an_undefined_releasable_is_refused(self, tmp_path):
        write_workspace(
            tmp_path, MIRRORED_MEMBER.replace('releasable = "cli"', 'releasable = "gone"')
        )
        with pytest.raises(MigrationError) as exc:
            plan_for(tmp_path, dev_node=True)
        assert "gone" in str(exc.value)

    def test_a_multi_member_releasable_is_refused(self, tmp_path):
        write_workspace(
            tmp_path,
            MIRRORED_MEMBER
            + '\n[[projects]]\npath = "pkgs/extra"\nname = "extra"\n'
            'releasable = "cli"\n',
        )
        with pytest.raises(MigrationError) as exc:
            plan_for(tmp_path, dev_node=True)
        message = str(exc.value)
        assert "cli" in message and "extra" in message

    def test_nothing_is_written_when_the_relocation_is_refused(self, tmp_path):
        write_workspace(
            tmp_path, MIRRORED_MEMBER.replace('releasable = "cli"', 'releasable = "gone"')
        )
        before = workspace_path(tmp_path).read_bytes()
        code, _output = migrate(tmp_path, dev_node=True)
        assert code == 2
        assert workspace_path(tmp_path).read_bytes() == before


# ---------------------------------------------------------------------------
# The whole old model at once
# ---------------------------------------------------------------------------


class TestFullMigration:
    @pytest.fixture
    def repo(self, tmp_path):
        write_workspace(tmp_path, OLD_MODEL)
        return tmp_path

    def test_the_result_loads(self, repo):
        code, output = migrate(repo, dev_node=True)
        assert code == 0
        assert "loads" in output

    def test_every_edit_is_applied(self, repo):
        migrate(repo, dev_node=True)
        data = read_workspace(repo)
        assert root_member(data)["dev_only"] is True
        assert all("watch" not in p for p in data["projects"])
        assert all("subtree_remote" not in p for p in data["projects"])
        cli = next(r for r in data["releasables"] if r["name"] == "cli")
        assert cli["subtree_remote"] == "git@github.com:owner/cli.git"


# ---------------------------------------------------------------------------
# Residue: what the script cannot decide
# ---------------------------------------------------------------------------


class TestResidue:
    """A loader error that survives the edits is printed verbatim."""

    @pytest.fixture
    def repo(self, tmp_path):
        # A non-root member holding the reserved name: which member owns the
        # repository root is exactly the decision the script will not make.
        write_workspace(
            tmp_path,
            '[[releasables]]\nname = "core"\ntag_format = "{name}@v{version}"\n\n'
            '[[projects]]\npath = "pkgs/core"\nname = "root"\n'
            'watch = ["pkgs/core/**"]\nreleasable = "core"\n',
        )
        return tmp_path

    def test_the_mechanical_edits_are_still_applied(self, repo):
        migrate(repo, dev_node=True)
        data = read_workspace(repo)
        assert all("watch" not in p for p in data["projects"])
        assert any(p["path"] == "." for p in data["projects"])

    def test_the_loader_error_is_printed_verbatim(self, repo):
        code, output = migrate(repo, dev_node=True)
        assert code == 1
        assert "OPERATOR INPUT REQUIRED" in output
        assert "reserved for the member that owns the repository root" in output

    def test_the_backfill_is_not_run_on_a_workspace_that_does_not_load(self, repo):
        _code, output = migrate(repo, dev_node=True, backfill=True)
        assert "backfill" in output.lower()
        assert "skipped" in output.lower()


# ---------------------------------------------------------------------------
# Idempotency and dry run
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.fixture
    def repo(self, tmp_path):
        write_workspace(tmp_path, OLD_MODEL)
        return tmp_path

    def test_second_run_proposes_nothing(self, repo):
        migrate(repo, dev_node=True)
        assert plan_for(repo, dev_node=True).edits == []

    def test_second_run_writes_nothing(self, repo):
        migrate(repo, dev_node=True)
        before = workspace_path(repo).read_bytes()
        code, output = migrate(repo, dev_node=True)
        assert code == 0
        assert workspace_path(repo).read_bytes() == before
        assert "nothing to migrate" in output.lower()

    def test_the_releasable_kind_is_idempotent_too(self, tmp_path):
        write_workspace(tmp_path, NO_ROOT_MEMBER)
        migrate(tmp_path, releasable="monorepo", tag_format="v{version}")
        before = workspace_path(tmp_path).read_bytes()
        code, _output = migrate(tmp_path, releasable="monorepo", tag_format="v{version}")
        assert code == 0
        assert workspace_path(tmp_path).read_bytes() == before


class TestDryRun:
    @pytest.fixture
    def repo(self, tmp_path):
        write_workspace(tmp_path, OLD_MODEL)
        return tmp_path

    def test_nothing_is_written(self, repo):
        before = workspace_path(repo).read_bytes()
        code, output = migrate(repo, dev_node=True, dry_run=True)
        assert code == 0
        assert workspace_path(repo).read_bytes() == before
        assert "--dry-run: nothing written." in output

    def test_the_plan_is_the_one_the_real_run_applies(self, repo):
        _code, preview = migrate(repo, dev_node=True, dry_run=True)
        _code, real = migrate(repo, dev_node=True)
        preview_edits = [ln for ln in preview.splitlines() if ln.startswith("  projects")]
        real_edits = [ln for ln in real.splitlines() if ln.startswith("  projects")]
        assert preview_edits == real_edits

    def test_the_verification_is_deferred_to_the_real_run(self, repo):
        _code, output = migrate(repo, dev_node=True, dry_run=True)
        assert "never written" in output

    def test_an_already_migrated_workspace_is_verified_under_dry_run(self, repo):
        migrate(repo, dev_node=True)
        _code, output = migrate(repo, dev_node=True, dry_run=True)
        assert "loads" in output
