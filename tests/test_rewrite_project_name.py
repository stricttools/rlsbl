"""``rlsbl rewrite project-name``: renaming a standalone project's published identity.

The command renames the identities rlsbl owns -- the package name in every
target manifest whose target can rename it, and the Go module path -- records
one ``identity-transition`` event per changed identity, and prints the steps it
leaves to the caller. Everything here drives the real CLI dispatch on a real git
repository, because the command commits.

The fixture is a standalone project with npm, PyPI and Go targets that has
released 0.27.1 under ``github.com/o/widget`` and has since moved its module to
``github.com/neworg/widget`` without releasing. The Go event must therefore
record the LAST PUBLISHED path, not the working tree's.
"""

import json
import os
import shlex
import subprocess

import pytest

import rlsbl
from conftest import archive_release
from githarness import git, harness_env, init_repo
from rlsbl.transition_record import (
    KIND_IDENTITY_TRANSITION,
    get_transition_record_path,
    read_events,
)

PUBLISHED_MODULE = "github.com/o/widget"
MOVED_MODULE = "github.com/neworg/widget"
RENAMED_MODULE = "github.com/neworg/gadget"

ALL_TARGETS = ("go", "npm", "pypi")


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _commit_all(root, message):
    """Commit every change in the fixture, named path by path."""
    # Not the harness's git(): it strips the output, and with it the leading
    # space of the first porcelain entry.
    status = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=str(root), capture_output=True, text=True, check=True,
        env=harness_env(),
    ).stdout
    paths = [entry[3:] for entry in status.split("\0") if entry]
    if paths:
        git(root, "add", "--", *paths)
        git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def _config(targets):
    entries = []
    pipelines = {}
    for name in targets:
        if name == "go":
            entries.append("go")
            pipelines["go"] = {
                "type": "go", "target": "go", "local": True,
                "install_paths": ["./cmd/widget"], "artifact": "binary",
            }
        elif name == "deno":
            entries.append("deno")
        else:
            entries.append({"name": name, "path": f"{name}/"})
            pipelines[name] = {"type": name, "target": name, "local": False}
    return {
        "targets": entries,
        "pipelines": pipelines,
        "publish_mode": "ci",
        "batch_limits": {"exclusions": []},
    }


def _write_project(root, *, version, module, targets):
    _write(root, ".rlsbl/config.json", json.dumps(_config(targets), indent=2) + "\n")
    _write(root, ".rlsbl/changes/unreleased.jsonl", "")
    _write(root, "VERSION", f"{version}\n")
    if "go" in targets:
        _write(root, "go.mod", f"module {module}\n\ngo 1.22\n")
        _write(root, "cmd/widget/main.go", (
            "package main\n\n"
            f'import "{module}/internal/core"\n\n'
            "func main() { _ = core.X }\n"
        ))
        _write(root, "internal/core/core.go", "package core\n\nvar X = 1\n")
    if "npm" in targets:
        _write(root, "npm/package.json", json.dumps({
            "name": "widget",
            "version": version,
            "description": "the widget tool",
            "bin": {"widget": "bin/widget"},
        }, indent=2) + "\n")
    if "pypi" in targets:
        _write(root, "pypi/pyproject.toml", (
            "[project]\n"
            'name = "widget"\n'
            f'version = "{version}"\n'
            'description = "the widget tool"\n'
            "\n"
            "[project.scripts]\n"
            'widget = "widget:main"\n'
        ))
        _write(root, "pypi/widget/__init__.py", "def main():\n    pass\n")
    if "deno" in targets:
        _write(root, "deno.json", json.dumps(
            {"name": "widget", "version": version}, indent=2,
        ) + "\n")


def _write_release_file(root, *, targets, bump="minor"):
    include = ", ".join(f'"{t}"' for t in targets)
    _write(root, ".rlsbl/releases/unreleased.toml", (
        "format_version = 1\n"
        f'bump = "{bump}"\n'
        'description = "the next release"\n'
        f"include = [{include}]\n"
        "exclude = []\n"
    ))


def _move_module(root, old, new):
    for rel in ("go.mod", "cmd/widget/main.go"):
        path = root / rel
        path.write_text(path.read_text().replace(old, new))


def build_released(root, *, targets=ALL_TARGETS, current_module=MOVED_MODULE):
    """Released 0.27.1 under PUBLISHED_MODULE, then moved to *current_module*."""
    init_repo(root)
    _write_project(root, version="0.27.1", module=PUBLISHED_MODULE, targets=targets)
    release_sha = _commit_all(root, "v0.27.1")
    git(root, "tag", "v0.27.1")
    if "go" in targets and current_module != PUBLISHED_MODULE:
        _move_module(root, PUBLISHED_MODULE, current_module)
    archive_release(root / ".rlsbl" / "releases", "0.27.1", release_sha)
    _write_release_file(root, targets=targets)
    _commit_all(root, "move on after the release")
    return root


def build_never_released(root, *, targets=ALL_TARGETS):
    init_repo(root)
    _write_project(root, version="0.1.0", module=MOVED_MODULE, targets=targets)
    _write_release_file(root, targets=targets)
    _commit_all(root, "initial")
    return root


def rename(root, monkeypatch, *extra, frm="widget", to="gadget", dry_run=False):
    monkeypatch.chdir(root)
    argv = ["rewrite", "project-name", "--from", frm, "--to", to,
            "--approve-consequential", *extra]
    if dry_run:
        argv.insert(0, "--dry-run")
    return rlsbl.app.test(argv)


def identity_events(root):
    path = get_transition_record_path(str(root))
    if not os.path.exists(path):
        return []
    return [
        (e.facet, e.old, e.new, e.effective_version)
        for e in read_events(path, kinds=[KIND_IDENTITY_TRANSITION])
    ]


def commit_count(root):
    return int(git(root, "rev-list", "--count", "HEAD"))


def snapshot(root):
    out = {}
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in names:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, root)] = f.read()
    return out


@pytest.fixture
def released(tmp_path):
    return build_released(tmp_path / "widget")


# ---------------------------------------------------------------------------
# The rename, end to end
# ---------------------------------------------------------------------------


class TestRenameEndToEnd:
    def test_every_identity_is_renamed_and_recorded(self, released, monkeypatch):
        before = commit_count(released)
        result = rename(released, monkeypatch)
        assert result.exit_code == 0, result.stderr + result.stdout

        npm = json.loads((released / "npm/package.json").read_text())
        assert npm["name"] == "gadget"
        # Command names are not package names: bin is left alone.
        assert npm["bin"] == {"widget": "bin/widget"}
        pyproject = (released / "pypi/pyproject.toml").read_text()
        assert 'name = "gadget"' in pyproject
        assert 'widget = "widget:main"' in pyproject
        assert (released / "go.mod").read_text().startswith(f"module {RENAMED_MODULE}\n")
        assert f'"{RENAMED_MODULE}/internal/core"' in (
            released / "cmd/widget/main.go").read_text()

        assert sorted(identity_events(released)) == sorted([
            ("package-name", "widget", "gadget", "0.28.0"),
            ("go-module-path", PUBLISHED_MODULE, RENAMED_MODULE, "0.28.0"),
        ])
        # The rename and the record are two commits, and nothing is left over.
        assert commit_count(released) == before + 2
        assert git(released, "status", "--porcelain") == ""

    def test_source_directories_are_not_moved(self, released, monkeypatch):
        result = rename(released, monkeypatch)
        assert result.exit_code == 0, result.stderr
        assert (released / "cmd/widget/main.go").is_file()
        assert (released / "pypi/widget/__init__.py").is_file()
        assert not (released / "cmd/gadget").exists()

    def test_closing_message_lists_the_remaining_steps_in_order(
        self, released, monkeypatch,
    ):
        result = rename(released, monkeypatch)
        assert result.exit_code == 0, result.stderr
        out = result.stdout
        steps = [
            "cmd/widget",
            "pypi/widget",
            "code that imports",
            "install_paths",
            "rlsbl scaffold",
            'npm/package.json bin "widget" still names the old command',
            'pypi/pyproject.toml [project.scripts] "widget" still names the old command',
            "GitHub repository",
            "rlsbl changelog add",
        ]
        positions = [out.find(step) for step in steps]
        assert all(p >= 0 for p in positions), (steps, positions, out)
        assert positions == sorted(positions), out

    def test_the_changelog_command_it_names_runs(self, released, monkeypatch):
        result = rename(released, monkeypatch)
        assert result.exit_code == 0, result.stderr
        line = next(
            ln.strip() for ln in result.stdout.splitlines()
            if "rlsbl changelog add" in ln
        )
        command = line[line.index("rlsbl changelog add"):]
        argv = shlex.split(command)[1:]
        assert "--type" in argv and "breaking" in argv, argv
        ran = rlsbl.app.test(argv)
        assert ran.exit_code == 0, ran.stderr + ran.stdout
        entries = (released / ".rlsbl/changes/unreleased.jsonl").read_text()
        assert '"breaking"' in entries

    def test_the_scaffold_command_it_names_exists(self, released, monkeypatch):
        monkeypatch.chdir(released)
        assert rlsbl.app.test(["scaffold", "--help"]).exit_code == 0


class TestEffectiveVersion:
    def test_released_project_gets_current_plus_bump(self, released, monkeypatch):
        result = rename(released, monkeypatch)
        assert result.exit_code == 0, result.stderr
        assert {e[3] for e in identity_events(released)} == {"0.28.0"}

    def test_never_released_project_gets_its_current_version(
        self, tmp_path, monkeypatch,
    ):
        root = build_never_released(tmp_path / "widget")
        result = rename(root, monkeypatch)
        assert result.exit_code == 0, result.stderr + result.stdout
        # The first release ships the current version as-is, bump ignored.
        assert identity_events(root) == [
            ("package-name", "widget", "gadget", "0.1.0"),
        ]
        assert "never released" in result.stdout
        # The module path is still renamed in the working tree.
        assert (root / "go.mod").read_text().startswith(f"module {RENAMED_MODULE}\n")


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_writes_nothing_and_reports_counts(self, released, monkeypatch):
        before_files = snapshot(released)
        before_commits = commit_count(released)
        result = rename(released, monkeypatch, dry_run=True)
        assert result.exit_code == 0, result.stderr
        out = result.stdout
        assert "npm/package.json: rewrite: 1 occurrence" in out, out
        assert "pypi/pyproject.toml: rewrite: 1 occurrence" in out, out
        assert "go.mod: rewrite: 1 occurrence" in out, out
        assert "cmd/widget/main.go: rewrite: 1 occurrence" in out, out
        assert f"package-name widget -> gadget, effective 0.28.0" in out, out
        assert (
            f"go-module-path {PUBLISHED_MODULE} -> {RENAMED_MODULE}, "
            f"effective 0.28.0"
        ) in out, out
        assert snapshot(released) == before_files
        assert commit_count(released) == before_commits
        assert identity_events(released) == []


# ---------------------------------------------------------------------------
# A crash between the two commits is completed by re-running
# ---------------------------------------------------------------------------


class TestRerun:
    def test_crash_between_commits_is_completed_by_rerunning(
        self, released, monkeypatch,
    ):
        from rlsbl.commands.rewrite import project_name

        before = commit_count(released)

        def crash(*args, **kwargs):
            raise RuntimeError("simulated crash before the record")

        with monkeypatch.context() as m:
            m.setattr(project_name, "record_identity_transitions", crash)
            with pytest.raises(RuntimeError, match="simulated crash"):
                rename(released, monkeypatch)
        assert commit_count(released) == before + 1
        assert identity_events(released) == []
        assert json.loads(
            (released / "npm/package.json").read_text())["name"] == "gadget"

        healed = rename(released, monkeypatch)
        assert healed.exit_code == 0, healed.stderr + healed.stdout
        assert commit_count(released) == before + 2
        assert sorted(identity_events(released)) == sorted([
            ("package-name", "widget", "gadget", "0.28.0"),
            ("go-module-path", PUBLISHED_MODULE, RENAMED_MODULE, "0.28.0"),
        ])

    def test_rerun_after_completion_changes_nothing(self, released, monkeypatch):
        assert rename(released, monkeypatch).exit_code == 0
        after = commit_count(released)
        again = rename(released, monkeypatch)
        assert again.exit_code == 0, again.stderr
        assert commit_count(released) == after
        assert len(identity_events(released)) == 2


# ---------------------------------------------------------------------------
# Changelog coverage: the rename is a user-visible change, the record is not
# ---------------------------------------------------------------------------


def _commit_by_message(root, message):
    return git(root, "log", "-1", "--format=%H", "--fixed-strings",
               f"--grep={message}")


def _is_autogenerated(root, sha):
    return "Autogenerated: true" in git(root, "log", "-1", "--format=%B", sha)


def _coverage(root, monkeypatch):
    monkeypatch.chdir(root)
    return rlsbl.app.test(["check", "--name", "changelog-coverage"])


class TestChangelogCoverage:
    def test_rename_commit_needs_the_entry_the_closing_message_prints(
        self, released, monkeypatch,
    ):
        # Cover the fixture's own post-release commit, so the only commit the
        # coverage check can flag afterwards is one the rename made.
        monkeypatch.chdir(released)
        covered = rlsbl.app.test([
            "changelog", "add", "--commits", git(released, "rev-parse", "HEAD"),
            "--no-user-facing",
        ])
        assert covered.exit_code == 0, covered.stderr + covered.stdout
        assert _coverage(released, monkeypatch).exit_code == 0

        result = rename(released, monkeypatch)
        assert result.exit_code == 0, result.stderr + result.stdout

        rename_sha = _commit_by_message(
            released, "rewrite: rename project widget -> gadget")
        record_sha = _commit_by_message(
            released, "rewrite: record the widget -> gadget identity transitions")
        assert rename_sha and record_sha
        assert not _is_autogenerated(released, rename_sha)
        assert _is_autogenerated(released, record_sha)

        flagged = _coverage(released, monkeypatch)
        assert flagged.exit_code != 0, flagged.stdout
        assert rename_sha[:7] in flagged.stdout + flagged.stderr

        line = next(
            ln.strip() for ln in result.stdout.splitlines()
            if "rlsbl changelog add" in ln
        )
        argv = shlex.split(line[line.index("rlsbl changelog add"):])[1:]
        assert argv[argv.index("--commits") + 1] == rename_sha[:12]
        ran = rlsbl.app.test(argv)
        assert ran.exit_code == 0, ran.stderr + ran.stdout

        cleared = _coverage(released, monkeypatch)
        assert cleared.exit_code == 0, cleared.stdout + cleared.stderr


# ---------------------------------------------------------------------------
# Refusals, each with its fix performed
# ---------------------------------------------------------------------------


def _refused(result, *needles):
    assert result.exit_code == 1, result.stdout + result.stderr
    for needle in needles:
        assert needle in result.stderr, result.stderr


class TestRefusals:
    def test_workspace_refuses_naming_rename_releasable(
        self, released, monkeypatch,
    ):
        _write(released, ".rlsbl-monorepo/workspace.toml", "")
        _commit_all(released, "a workspace")
        before = snapshot(released)
        result = rename(released, monkeypatch)
        _refused(result, "rlsbl monorepo rename-releasable")
        assert snapshot(released) == before
        # The fix is a different command; it exists with that spelling.
        assert rlsbl.app.test(
            ["monorepo", "rename-releasable", "--help"]).exit_code == 0

    def test_from_that_no_manifest_declares_refuses_until_corrected(
        self, released, monkeypatch,
    ):
        before = snapshot(released)
        result = rename(released, monkeypatch, frm="wigdet")
        _refused(result, "npm/package.json", '"widget"', "--from widget")
        assert snapshot(released) == before
        assert rename(released, monkeypatch, frm="widget").exit_code == 0

    def test_equal_names_refuse_until_a_different_to(self, released, monkeypatch):
        result = rename(released, monkeypatch, to="widget")
        _refused(result, "--from and --to are the same")
        assert rename(released, monkeypatch, to="gadget").exit_code == 0

    def test_invalid_npm_name_refuses_until_valid(self, tmp_path, monkeypatch):
        root = build_released(tmp_path / "widget", targets=("npm",))
        result = rename(root, monkeypatch, to="Gadget")
        _refused(result, "npm", "uppercase")
        assert rename(root, monkeypatch, to="gadget").exit_code == 0

    def test_invalid_pypi_name_refuses_until_valid(self, tmp_path, monkeypatch):
        root = build_released(tmp_path / "widget", targets=("pypi",))
        result = rename(root, monkeypatch, to="gadget-")
        _refused(result, "PyPI", "PEP 508")
        assert rename(root, monkeypatch, to="gadget").exit_code == 0

    def test_discouraged_go_name_refuses_naming_the_clean_spelling(
        self, tmp_path, monkeypatch,
    ):
        root = build_released(tmp_path / "widget", targets=("go",))
        result = rename(root, monkeypatch, to="gad_get")
        _refused(result, "underscore", "--to gadget")
        assert rename(root, monkeypatch, to="gadget").exit_code == 0
        assert (root / "go.mod").read_text().startswith(f"module {RENAMED_MODULE}\n")

    def test_dirty_tree_refuses_until_committed(self, released, monkeypatch):
        _write(released, "notes.txt", "stray\n")
        result = rename(released, monkeypatch)
        _refused(result, "notes.txt")
        _commit_all(released, "notes")
        assert rename(released, monkeypatch).exit_code == 0

    def test_missing_release_file_refuses_naming_release_init(
        self, released, monkeypatch,
    ):
        git(released, "rm", "-q", ".rlsbl/releases/unreleased.toml")
        git(released, "commit", "-q", "-m", "drop the release file")
        result = rename(released, monkeypatch)
        _refused(result, "rlsbl release init")

        monkeypatch.chdir(released)
        init = rlsbl.app.test(["release", "init"])
        assert init.exit_code == 0, init.stderr
        path = released / ".rlsbl/releases/unreleased.toml"
        text = path.read_text()
        text = text.replace('bump = ""', 'bump = "minor"', 1)
        text = text.replace('description = ""', 'description = "next"', 1)
        path.write_text(text)
        _commit_all(released, "release file")
        result = rename(released, monkeypatch)
        assert result.exit_code == 0, result.stderr
        assert {e[3] for e in identity_events(released)} == {"0.28.0"}

    def test_unsupported_target_still_naming_from_refuses_until_edited(
        self, tmp_path, monkeypatch,
    ):
        root = build_released(
            tmp_path / "widget", targets=("go", "npm", "pypi", "deno"),
        )
        result = rename(root, monkeypatch)
        _refused(result, "deno", 'deno.json "name"')
        deno = root / "deno.json"
        deno.write_text(deno.read_text().replace('"widget"', '"gadget"'))
        _commit_all(root, "rename the deno manifest by hand")
        result = rename(root, monkeypatch)
        assert result.exit_code == 0, result.stderr + result.stdout

    def test_go_path_not_ending_in_from_refuses_naming_go_module_path(
        self, tmp_path, monkeypatch,
    ):
        root = build_released(
            tmp_path / "widget", current_module="github.com/neworg/widget-go",
        )
        result = rename(root, monkeypatch)
        _refused(result, "rlsbl rewrite go-module-path",
                 "--from-module github.com/neworg/widget-go")

        monkeypatch.chdir(root)
        moved = rlsbl.app.test([
            "rewrite", "go-module-path",
            "--from-module", "github.com/neworg/widget-go",
            "--to-module", RENAMED_MODULE,
        ])
        assert moved.exit_code == 0, moved.stderr
        _commit_all(root, "move the module by hand")
        result = rename(root, monkeypatch)
        assert result.exit_code == 0, result.stderr + result.stdout
        assert ("go-module-path", PUBLISHED_MODULE, RENAMED_MODULE, "0.28.0") in (
            identity_events(root))


# ---------------------------------------------------------------------------
# A latest release whose commit is unrecoverable
# ---------------------------------------------------------------------------


def build_unrecoverable(root, *, targets=ALL_TARGETS):
    """Released 0.27.1 under PUBLISHED_MODULE, but the archive is marked
    unrecoverable: no tag and no version-bump commit named the commit it
    shipped from when the archives were backfilled."""
    init_repo(root)
    _write_project(root, version="0.27.1", module=PUBLISHED_MODULE, targets=targets)
    _commit_all(root, "the work 0.27.1 shipped")
    if "go" in targets:
        _move_module(root, PUBLISHED_MODULE, MOVED_MODULE)
    archive_release(root / ".rlsbl" / "releases", "0.27.1", None, unrecoverable=True)
    _write_release_file(root, targets=targets)
    _commit_all(root, "move on after the release")


class TestUnrecoverableLatestRelease:
    def test_a_go_target_is_refused_naming_no_command(
        self, tmp_path, monkeypatch,
    ):
        # No command can honestly establish the commit an unrecoverable
        # release shipped from, so the refusal names none.
        root = tmp_path / "widget"
        build_unrecoverable(root)
        before = snapshot(root)
        count = commit_count(root)
        result = rename(root, monkeypatch)
        _refused(
            result, "0.27.1", "marked unrecoverable in its archive",
            "Go module path", "go-module-path event",
        )
        assert "rlsbl " not in result.stderr, result.stderr
        assert "`" not in result.stderr, result.stderr
        assert snapshot(root) == before
        assert commit_count(root) == count
        assert identity_events(root) == []

    def test_a_project_without_a_go_target_is_not_refused(
        self, tmp_path, monkeypatch,
    ):
        root = tmp_path / "widget"
        build_unrecoverable(root, targets=("npm", "pypi"))
        result = rename(root, monkeypatch)
        assert result.exit_code == 0, result.stdout + result.stderr
        assert identity_events(root) == [
            ("package-name", "widget", "gadget", "0.28.0"),
        ]
