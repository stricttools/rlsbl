"""Tests for the monorepo absorb and extract conversions.

Covers:
- require_filter_repo (installed vs not)
- Absorb: basic absorption, history rewrite, tag import, workspace.toml update
- Extract: the releasable-level conversion, reworked from the two commands it
  replaced. Scenarios that the collapse removed -- extracting one package, and
  the per-package changelog machinery the old code carried -- are pinned here as
  refusals and absences, so the retired shapes cannot quietly come back.

The extract conversion's own behavior is covered in
``tests/test_extract_conversion.py``; what stays here is the reworked form of
what these tests originally asserted.
"""

import json
import os
import pathlib
import shutil
import subprocess

import pytest

from conftest import DEFAULT_RELEASE_FILE, declared_members, make_releasable_monorepo, make_releasable_state, workspace_toml, make_workspace
from rlsbl.changelog.schema import ChangelogEntry, serialize_entry, parse_jsonl
from rlsbl.commands.monorepo import extract as extract_mod
from rlsbl.commands.monorepo.extract import (
    ExtractError,
    require_filter_repo,
    _run_git,
)
from rlsbl.commands.monorepo.absorb_cmd import AbsorbError, cmd_absorb
from rlsbl.commands.monorepo.extract_cmd import cmd_extract
from rlsbl.changelog.schema import parse_jsonl as _parse_jsonl_hashes
from rlsbl.workspace import (
    get_releasable_changes_dir,
    get_releasable_dir,
    load_workspace,
    load_releasables,
    Releasable,
    WorkspaceProject,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
)

# Check if git-filter-repo is available for integration tests
HAS_FILTER_REPO = shutil.which("git-filter-repo") is not None
skip_no_filter_repo = pytest.mark.skipif(
    not HAS_FILTER_REPO,
    reason="git-filter-repo not installed",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path, branch="main"):
    """Initialize a git repo with an initial commit."""
    subprocess.run(
        ["git", "init", "-q", "-b", branch],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.local"],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    readme = path / "README.md"
    readme.write_text("# test\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=str(path), check=True, capture_output=True, text=True,
    )


def _make_commit(path, filename, content="content", message="change"):
    """Make a commit and return the hash."""
    filepath = path / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content)
    subprocess.run(
        ["git", "add", str(filepath.relative_to(path))],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(path), check=True, capture_output=True, text=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _write_workspace(tmp_path, projects_toml, releasables_toml=""):
    """Write workspace.toml with raw TOML content."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir(exist_ok=True)
    content = releasables_toml + "\n" + projects_toml
    (ws_dir / WORKSPACE_FILE).write_text(workspace_toml(content))


def _write_changelog_entry(changes_dir, filename, entries):
    """Write changelog entries to a JSONL file."""
    os.makedirs(str(changes_dir), exist_ok=True)
    filepath = changes_dir / filename
    with open(str(filepath), "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(serialize_entry(entry) + "\n")


def _setup_monorepo(tmp_path):
    """Create a monorepo with two packages and changelog entries.

    Returns (root, commit_hashes) where commit_hashes is a dict of
    package_name -> list of commit hashes.
    """
    root = tmp_path / "monorepo"
    root.mkdir()
    _init_git_repo(root)

    # Create package directories
    (root / "pkgA").mkdir()
    (root / "pkgB").mkdir()

    # Write workspace.toml
    _write_workspace(root, """
[[projects]]
path = "pkgA"
name = "pkgA"
releasable = false

[[projects]]
path = "pkgB"
name = "pkgB"
releasable = false
""")

    # Create package files and commit
    hash_a1 = _make_commit(root, "pkgA/main.py", "print('A')", "add pkgA")
    hash_b1 = _make_commit(root, "pkgB/main.py", "print('B')", "add pkgB")

    # Create .rlsbl/changes for pkgA
    changes_a = root / "pkgA" / ".rlsbl" / "changes"
    _write_changelog_entry(changes_a, "unreleased.jsonl", [
        ChangelogEntry(
            commits=[hash_a1[:8]],
            user_facing=True,
            description="Added package A",
            type="feature",
        ),
    ])
    _make_commit(root, "pkgA/.rlsbl/changes/unreleased.jsonl", open(str(changes_a / "unreleased.jsonl")).read(), "changelog for pkgA")

    # Create .rlsbl/changes for pkgB
    changes_b = root / "pkgB" / ".rlsbl" / "changes"
    _write_changelog_entry(changes_b, "unreleased.jsonl", [
        ChangelogEntry(
            commits=[hash_b1[:8]],
            user_facing=True,
            description="Added package B",
            type="feature",
        ),
    ])
    _make_commit(root, "pkgB/.rlsbl/changes/unreleased.jsonl", open(str(changes_b / "unreleased.jsonl")).read(), "changelog for pkgB")

    # Create config.json for pkgA
    config_dir = root / "pkgA" / ".rlsbl"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"publish_mode": "ci", "targets": []}) + "\n")
    _make_commit(root, "pkgA/.rlsbl/config.json", json.dumps({"publish_mode": "ci", "targets": []}) + "\n", "config for pkgA")

    # Commit workspace
    subprocess.run(
        ["git", "add", WORKSPACE_DIR],
        cwd=str(root), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add workspace"],
        cwd=str(root), check=True, capture_output=True, text=True,
    )

    return root, {"pkgA": [hash_a1], "pkgB": [hash_b1]}


def _setup_source_repo(tmp_path):
    """Create a standalone repo suitable for absorbing into a monorepo.

    Returns (repo_path, commit_hash).
    """
    repo = tmp_path / "source_repo"
    repo.mkdir()
    _init_git_repo(repo)

    # Create some source files
    commit_hash = _make_commit(repo, "main.py", "print('source')", "add main")

    # Create .rlsbl/changes
    changes_dir = repo / ".rlsbl" / "changes"
    _write_changelog_entry(changes_dir, "unreleased.jsonl", [
        ChangelogEntry(
            commits=[commit_hash[:8]],
            user_facing=True,
            description="Source feature",
            type="feature",
        ),
    ])
    _make_commit(
        repo,
        ".rlsbl/changes/unreleased.jsonl",
        open(str(changes_dir / "unreleased.jsonl")).read(),
        "add changelog",
    )

    return repo, commit_hash


# ---------------------------------------------------------------------------
# require_filter_repo
# ---------------------------------------------------------------------------


class TestRequireFilterRepo:
    def test_returns_path_when_installed(self, monkeypatch):
        """When git-filter-repo is on PATH, return its path."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git-filter-repo")
        path = require_filter_repo()
        assert path == "/usr/bin/git-filter-repo"

    def test_raises_when_not_installed(self, monkeypatch):
        """When git-filter-repo is not on PATH, raise ExtractError."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(ExtractError, match="git-filter-repo is not installed"):
            require_filter_repo()

    def test_error_includes_install_instructions(self, monkeypatch):
        """The error message includes installation instructions."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(ExtractError) as exc_info:
            require_filter_repo()
        msg = str(exc_info.value)
        assert "pip install git-filter-repo" in msg
        assert "github.com/newren/git-filter-repo" in msg


# ---------------------------------------------------------------------------
# The package-level extract machinery, and its absence
# ---------------------------------------------------------------------------


class TestPackageLevelExtractMachineryIsGone:
    """The helpers a package-level extract needed no longer exist.

    Extraction operates on a RELEASABLE now, and a releasable owns its version,
    its changelog and its release archives as whole directories. That removed
    three jobs the old code did by hand, and each one is pinned absent here so
    it cannot be reintroduced piecemeal:

    * finding the PROJECT to extract (``_find_project``) -- the command resolves
      a releasable, and an unknown name is refused by name in
      ``test_extract_conversion.py``;
    * filtering changelog entries down to the ones "relevant to a package"
      (``_filter_changelog_entries``) -- a heuristic over ``packages`` fields
      and per-commit path lookups, replaced by moving the releasable's whole
      ``changes/`` directory;
    * copying those filtered entries into a new repository
      (``_migrate_changelog_to_new_repo``) and removing one project from
      workspace.toml (``_remove_project_from_workspace``) -- both subsumed by
      the state transplant and the single workspace edit the conversion commits.
    """

    @pytest.mark.parametrize("name", [
        "_find_project",
        "_filter_changelog_entries",
        "_migrate_changelog_to_new_repo",
        "_remove_project_from_workspace",
        "_create_rlsbl_config",
        "validate_extract_preconditions",
        "cmd_extract_releasable",
    ])
    def test_helper_is_gone(self, name):
        assert not hasattr(extract_mod, name)

    def test_extract_is_no_longer_exported_from_the_absorb_module(self):
        """``cmd_extract`` lives with the conversion it belongs to."""
        assert not hasattr(extract_mod, "cmd_extract")

    def test_a_package_outside_every_releasable_cannot_be_extracted(
        self, tmp_path, monkeypatch,
    ):
        """The old command took a package name; the new one takes a releasable.

        ``_setup_monorepo``'s members declare ``releasable = false``, which is
        exactly the shape the package-level command used to serve. Naming one of
        them now is naming something that is not a releasable, and the refusal
        says so and lists what is available.
        """
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git-filter-repo")
        root, _ = _setup_monorepo(tmp_path)

        with pytest.raises(ExtractError) as exc:
            cmd_extract(str(root), "pkgA", str(tmp_path / "out"), dry_run=True)
        assert "not found in this workspace" in str(exc.value)

    def test_target_exists_is_still_refused(self, tmp_path, monkeypatch):
        """The precondition survived the collapse, on the new command."""
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git-filter-repo")
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        target = tmp_path / "output"
        target.mkdir()

        with pytest.raises(ExtractError, match="target path already exists"):
            cmd_extract(str(root), "core", str(target), dry_run=True)


# ---------------------------------------------------------------------------
# Absorb's preconditions, on the observation that replaced them
# ---------------------------------------------------------------------------
#
# ``validate_absorb_preconditions`` is gone: the rebuilt conversion resolves and
# refuses everything during OBSERVATION, so a ``--dry-run`` refuses exactly what
# an apply would and neither has written anything by the time it does. Each
# assertion below is its predecessor's, re-aimed at ``cmd_absorb(dry_run=True)``.
# They need no git-filter-repo binary (``shutil.which`` is patched), which is why
# they stay here rather than moving to ``tests/test_absorb_conversion.py``.


def _clean_source(tmp_path, name="source"):
    """Create a clean, committed git source repo suitable for absorb."""
    repo = tmp_path / name
    repo.mkdir()
    _init_git_repo(repo)
    _make_commit(repo, "main.py", "print('src')", "add main")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
    )
    _make_commit(
        repo, "pyproject.toml", (repo / "pyproject.toml").read_text(), "manifest",
    )
    return repo


def _absorb_workspace(tmp_path, members=("existing",)):
    """A committed workspace with the given members, ready to absorb into."""
    root = tmp_path / "monorepo"
    root.mkdir()
    _init_git_repo(root)
    for member in members:
        (root / member).mkdir(exist_ok=True)
        _make_commit(root, f"{member}/keep.txt", "keep\n", f"add {member}")
    make_workspace(
        str(root),
        [WorkspaceProject({"path": m, "name": m}) for m in members],
    )
    subprocess.run(["git", "add", WORKSPACE_DIR], cwd=str(root),
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", "workspace"], cwd=str(root),
                   check=True, capture_output=True, text=True)
    return root


class TestAbsorbPreconditions:
    def test_valid_preconditions(self, tmp_path, monkeypatch):
        """Happy path: source clean git repo, path and name both free."""
        monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
        root = _absorb_workspace(tmp_path)
        source = _clean_source(tmp_path)

        preview = cmd_absorb(
            str(root), str(source), "pkgs/new", name="new_pkg", dry_run=True,
        )
        assert preview.by_key("releasable").state == "create_releasable"
        assert len(declared_members(load_workspace(str(root)))) == 1

    def test_no_filter_repo(self, tmp_path, monkeypatch):
        """Error when git-filter-repo is not installed."""
        monkeypatch.setattr(shutil, "which", lambda n: None)
        root = _absorb_workspace(tmp_path)
        source = _clean_source(tmp_path)

        with pytest.raises(ExtractError, match="git-filter-repo is not installed"):
            cmd_absorb(str(root), str(source), "new", name="new_pkg", dry_run=True)

    def test_source_not_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
        root = _absorb_workspace(tmp_path)

        with pytest.raises(AbsorbError, match="does not exist"):
            cmd_absorb(
                str(root), str(tmp_path / "nonexistent"), "new", name="pkg",
                dry_run=True,
            )

    def test_source_not_git_repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
        root = _absorb_workspace(tmp_path)
        source = tmp_path / "source"
        source.mkdir()

        with pytest.raises(AbsorbError, match="not a git repository"):
            cmd_absorb(str(root), str(source), "new", name="pkg", dry_run=True)

    def test_source_dirty_is_rejected(self, tmp_path, monkeypatch):
        """A source with uncommitted changes is a hard error (would be dropped)."""
        monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
        root = _absorb_workspace(tmp_path)
        source = _clean_source(tmp_path)
        (source / "uncommitted.txt").write_text("dirty\n")

        with pytest.raises(AbsorbError, match="uncommitted changes"):
            cmd_absorb(str(root), str(source), "new", name="pkg", dry_run=True)

    def test_duplicate_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
        root = _absorb_workspace(tmp_path)
        source = _clean_source(tmp_path)

        with pytest.raises(AbsorbError, match="package 'existing' already exists"):
            cmd_absorb(
                str(root), str(source), "different", name="existing", dry_run=True,
            )

    def test_duplicate_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
        root = _absorb_workspace(tmp_path)
        source = _clean_source(tmp_path)

        with pytest.raises(AbsorbError, match="path 'existing' already exists"):
            cmd_absorb(
                str(root), str(source), "existing", name="brand_new", dry_run=True,
            )

    def test_the_refusals_are_absorb_errors_and_still_extract_errors(self):
        """One conversion error type, so a caller of either keeps catching it."""
        assert issubclass(AbsorbError, ExtractError)


# ---------------------------------------------------------------------------
# The reworked extract scenarios (integration; need git-filter-repo)
# ---------------------------------------------------------------------------


@skip_no_filter_repo
class TestExtractOnTheSingleMemberCase:
    """What the package-level tests asserted, on the command that replaced them.

    A single-member releasable is the shape the old ``monorepo extract
    <package>`` served: one directory hoisted to the root of a flat repository.
    Each assertion below is its predecessor's, re-aimed.
    """

    def test_dry_run_creates_nothing(self, tmp_path):
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted"

        preview = cmd_extract(str(root), "extras", str(target), dry_run=True)

        assert preview.by_key("releasable").state == "extract_to_standalone"
        assert not target.exists()

    def test_the_member_is_hoisted_to_the_repository_root(self, tmp_path):
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted"

        cmd_extract(str(root), "extras", str(target))

        assert os.path.isdir(str(target / ".git"))
        assert os.path.isfile(str(target / "main.py"))
        assert not (target / "pkgC").exists()

    def test_the_changelog_arrives_in_the_standalone_home(self, tmp_path):
        """The whole ``changes/`` directory moves, not a filtered subset."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted"

        cmd_extract(str(root), "extras", str(target))

        changes_dir = target / ".rlsbl" / "changes"
        assert changes_dir.is_dir()
        assert (changes_dir / "unreleased.jsonl").is_file()
        # The released version's locked JSONL and its generated markdown came
        # along too -- the old per-package migration carried neither.
        assert (changes_dir / "0.1.0.jsonl").is_file()
        assert (changes_dir / "0.1.0.md").is_file()

    def test_the_config_is_the_releasables_own(self, tmp_path):
        """No config is synthesized: the releasable's own config moves."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted"

        cmd_extract(str(root), "extras", str(target))

        config_path = target / ".rlsbl" / "config.json"
        assert config_path.is_file()
        config = json.loads(config_path.read_text())
        assert "publish_mode" in config
        assert "private" not in config

    def test_source_workspace_loses_the_releasable_and_its_member(self, tmp_path):
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted"

        cmd_extract(str(root), "extras", str(target))

        projects = load_workspace(str(root))
        names = [p.name for p in projects]
        assert "pkgC" not in names
        assert "pkgA" in names and "pkgB" in names
        assert [r.name for r in load_releasables(str(root), projects)] == ["core"]

    def test_target_exists_error(self, tmp_path):
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted"
        target.mkdir()

        with pytest.raises(ExtractError, match="target path already exists"):
            cmd_extract(str(root), "extras", str(target))

    def test_foreign_tag_pruned_orphan_scheme_tag_kept(self, tmp_path):
        """Only tags matching another CURRENT member's glob are pruned; a
        scheme-parsing tag matching no live member (e.g. this releasable's own
        history under an old prefix) is KEPT, never destroyed.

        Both tags are planted at a commit that touches pkgC so they survive the
        filter-repo path filter and reach the translation step:
        - ``core@v1.0.0`` matches the live releasable core's glob -> pruned.
        - ``oldextras@v0.5.0`` matches no live member glob (the pre-rename
          shape) -> kept.
        """
        root, hashes = _setup_monorepo_with_releasable_state(tmp_path)
        pkgc_commit = hashes["pkgC"][0]
        _git_tag(root, "core@v1.0.0", ref=pkgc_commit)
        _git_tag(root, "oldextras@v0.5.0", ref=pkgc_commit)

        target = tmp_path / "extracted"
        cmd_extract(str(root), "extras", str(target))

        tags = _run_git(str(target), "tag", "-l").split()
        assert "core@v1.0.0" not in tags
        assert "oldextras@v0.5.0" in tags


# ---------------------------------------------------------------------------
# cmd_absorb (history-rewrite integration tests)
# ---------------------------------------------------------------------------


def _git_tag(repo, tag, ref="HEAD"):
    subprocess.run(
        ["git", "tag", tag, ref],
        cwd=str(repo), check=True, capture_output=True, text=True,
    )


def _make_multi_commit(repo, files, message):
    """Write several files and commit them together, returning the hash."""
    for rel, content in files.items():
        fp = repo / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        subprocess.run(
            ["git", "add", rel],
            cwd=str(repo), check=True, capture_output=True, text=True,
        )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(repo), check=True, capture_output=True, text=True,
    )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _archive_anchored(repo, version):
    """Archive ``version`` at HEAD, anchored to the commit and tree it ships.

    A real released repository has one of these per version, and the absorb
    carries them across -- remapping the commit through the rewrite and
    recomputing the tree at the member's new path -- so the fixture writes them
    the way a release does rather than leaving the state half-real.
    """
    from rlsbl.release_file import write_archived_release_file

    sha = _run_git(str(repo), "rev-parse", "HEAD")
    write_archived_release_file(
        str(repo / ".rlsbl" / "releases"), version,
        bump="minor", include=[], description=f"release {version}",
        candidate_sha=sha,
        tree_hashes={".": _run_git(str(repo), "rev-parse", "HEAD^{tree}")},
    )
    subprocess.run(["git", "add", ".rlsbl/releases"], cwd=str(repo),
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", f"chore: archive {version}"],
                   cwd=str(repo), check=True, capture_output=True, text=True)
    # The version tag stands on the ANCHORED commit, not on the archive commit
    # that records it -- which is what a real release does (it tags the
    # CI-verified candidate, and the finalization commits land on top of it),
    # and what the ledger checks the two against each other for.
    return sha


def _setup_released_source_repo(tmp_path):
    """Build a released npm source repo: 2 version tags, finalized JSONL,
    anchored release archives, plus one unreleased entry.
    Returns (repo_path, {"c1": ..., ...}).
    """
    repo = tmp_path / "widget_src"
    repo.mkdir()
    _init_git_repo(repo)

    os.makedirs(str(repo / ".rlsbl"), exist_ok=True)
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["npm"]}) + "\n"
    )
    # v0.1.0 feature commit
    c1 = _make_multi_commit(
        repo,
        {
            "package.json": json.dumps({"name": "widget", "version": "0.1.0"}) + "\n",
            "src/index.js": "export const v = 1;\n",
            ".rlsbl/config.json": json.dumps({"publish_mode": "ci", "targets": ["npm"]}) + "\n",
        },
        "feat: initial widget",
    )
    changes = repo / ".rlsbl" / "changes"
    _write_changelog_entry(changes, "0.1.0.jsonl", [
        ChangelogEntry(commits=[c1[:8]], user_facing=True,
                       description="Initial widget", type="feature"),
    ])
    _make_commit(repo, ".rlsbl/changes/0.1.0.jsonl",
                 (changes / "0.1.0.jsonl").read_text(), "changelog 0.1.0")
    _git_tag(repo, "v0.1.0", ref=_archive_anchored(repo, "0.1.0"))

    # v0.2.0 feature commit
    c3 = _make_multi_commit(
        repo,
        {
            "package.json": json.dumps({"name": "widget", "version": "0.2.0"}) + "\n",
            "src/feature.js": "export const f = 2;\n",
        },
        "feat: v0.2.0 feature",
    )
    _write_changelog_entry(changes, "0.2.0.jsonl", [
        ChangelogEntry(commits=[c3[:8]], user_facing=True,
                       description="Widget feature", type="feature"),
    ])
    _make_commit(repo, ".rlsbl/changes/0.2.0.jsonl",
                 (changes / "0.2.0.jsonl").read_text(), "changelog 0.2.0")
    _git_tag(repo, "v0.2.0", ref=_archive_anchored(repo, "0.2.0"))

    # Unreleased work
    c5 = _make_commit(repo, "src/wip.js", "export const w = 3;\n", "feat: wip")
    _write_changelog_entry(changes, "unreleased.jsonl", [
        ChangelogEntry(commits=[c5[:8]], user_facing=True,
                       description="Work in progress", type="feature"),
    ])
    _make_commit(repo, ".rlsbl/changes/unreleased.jsonl",
                 (changes / "unreleased.jsonl").read_text(), "changelog wip")

    return repo, {"c1": c1, "c3": c3, "c5": c5}


def _setup_plain_monorepo(tmp_path):
    """A minimal committed monorepo (no pre-existing package conflicts).

    ``existing`` stands outside every releasable: the loader requires each
    member to say which releasable it belongs to, and this one belongs to none.
    """
    root = tmp_path / "mono"
    root.mkdir()
    _init_git_repo(root)
    (root / "existing").mkdir()
    _write_workspace(root, """
[[projects]]
path = "existing"
name = "existing"
releasable = false
""")
    _make_commit(root, "existing/keep.txt", "keep\n", "add existing")
    subprocess.run(["git", "add", WORKSPACE_DIR], cwd=str(root),
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", "workspace"], cwd=str(root),
                   check=True, capture_output=True, text=True)
    return root


@skip_no_filter_repo
class TestCmdAbsorbDryRun:
    def test_dry_run_zero_mutations(self, tmp_path):
        """Dry run renders the plan and mutates nothing.

        REWORKED: the command returns a Preview rather than a dict of what it
        would have done, so the tags it would import are read off the plan's
        own tag item instead of a ``tags_to_import`` key.
        """
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        head_before = _run_git(str(root), "rev-parse", "HEAD")

        preview = cmd_absorb(
            str(root), str(source), "packages/widget", dry_run=True,
        )
        facts = "\n".join(preview.by_key("tags").facts)
        assert "v0.1.0 -> widget@v0.1.0" in facts
        assert "v0.2.0 -> widget@v0.2.0" in facts

        # Nothing changed: no new commit, no new project, no new dir.
        assert _run_git(str(root), "rev-parse", "HEAD") == head_before
        names = [p.name for p in load_workspace(str(root))]
        assert "widget" not in names
        assert not (root / "packages" / "widget").exists()


@skip_no_filter_repo
class TestAbsorbHistoryRewrite:
    def test_full_history_rewrite(self, tmp_path, monkeypatch):
        """REWORKED on two counts.

        The changelog now lands in the releasable's state directory (an
        absorbed repository arrives as a releasable, and its changelog has one
        home), and the source's own ``v0.2.0`` is KEPT as the boundary alias
        beside ``widget@v0.2.0`` rather than deleted -- the old code deleted
        every bare tag it found, including tags the destination already owned.
        """
        root = _setup_plain_monorepo(tmp_path)
        source, hashes = _setup_released_source_repo(tmp_path)

        cmd_absorb(str(root), str(source), "packages/widget")

        # 1. git log for the dest path shows full rewritten history.
        log = _run_git(str(root), "log", "--oneline", "--", "packages/widget")
        assert "initial widget" in log
        assert "v0.2.0 feature" in log
        assert "wip" in log

        # 2. describe resolves the imported monorepo-scheme tag.
        desc = _run_git(str(root), "describe", "--tags", "--match", "widget@v*")
        assert desc.startswith("widget@v0.2.0")

        # 3. every JSONL hash resolves in monorepo history.
        changes_dir = pathlib.Path(get_releasable_changes_dir(str(root), "widget"))
        all_hashes = []
        for jf in changes_dir.glob("*.jsonl"):
            for entry in _parse_jsonl_hashes(str(jf)):
                all_hashes.extend(entry.commits)
        assert all_hashes  # sanity: there are hashes to check
        for h in all_hashes:
            # cat-file -e raises via check=True if the object is missing.
            _run_git(str(root), "cat-file", "-e", h + "^{commit}")

        # 4. the monorepo-scheme tags are there, and the pre-conversion name of
        #    the CURRENT version stays resolvable as the boundary alias.
        tags = _run_git(str(root), "tag", "-l").split()
        assert "widget@v0.1.0" in tags
        assert "widget@v0.2.0" in tags
        assert "v0.2.0" in tags
        assert "v0.1.0" not in tags  # one alias, at the current version only

    def test_compute_release_version_bumps_forward(self, tmp_path, monkeypatch):
        """With the imported tags and the migrated ledger, the next release is
        a forward bump -- the destroyed-tag guard does not fire.

        REWORKED: the absorbed unit is a RELEASABLE, so the version and the
        ledger are read from the releasable's state directory and the tag comes
        from its declared format. Under the old absorb this call read a
        per-package manifest and a per-package releases directory that held no
        archive at all.
        """
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        cmd_absorb(str(root), str(source), "packages/widget")

        from rlsbl.commands.release.validate import compute_release_version
        import rlsbl.commands.release as release_mod
        from rlsbl.utils import RemoteTagResult, RemoteTagState
        from rlsbl.targets import TARGETS, detect_targets

        monkeypatch.setattr(
            release_mod, "remote_tag_commit",
            lambda tag, **kw: RemoteTagResult(state=RemoteTagState.ABSENT),
        )
        monkeypatch.chdir(str(root))

        dest_full = os.path.join(str(root), "packages", "widget")
        entry = detect_targets(dest_full)[0]
        target = TARGETS[entry.name]

        cur, new, bump, tag = compute_release_version(
            target, entry.path, "patch", "widget", "packages/widget",
            lambda *a, **k: None, project_dir=dest_full,
            workspace_root=str(root), releasable_name="widget",
            releasable_tag_fmt="{name}@v{version}",
        )
        assert cur == "0.2.0"
        assert new == "0.2.1"  # patch bump forward, NOT a re-release of 0.2.0
        assert bump == "patch"
        assert tag == "widget@v0.2.1"

    def test_changelog_coverage_passes(self, tmp_path, monkeypatch):
        """All unreleased package commits are covered with zero hand-fixups."""
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        cmd_absorb(str(root), str(source), "packages/widget")

        from rlsbl.changelog.validate import check_coverage
        from rlsbl.ownership import OwnershipScope

        # The glob is the RELEASABLE's now, not the member target's.
        tag_glob = "widget@v*"

        members = load_workspace(str(root))
        proj = next((p for p in members if p.name == "widget"), None)
        assert proj is not None

        changes_dir = pathlib.Path(get_releasable_changes_dir(str(root), "widget"))
        entries = _parse_jsonl_hashes(str(changes_dir / "unreleased.jsonl"))

        # FLIPPED: the range is bounded by the LEDGER, and absorb now MIGRATES
        # the source's release archives rather than importing only its tags --
        # so the ledger is already there, remapped onto the rewritten commits,
        # and this test no longer has to fabricate one.
        assert (changes_dir.parent / "releases" / "v0.2.0.toml").is_file()

        monkeypatch.chdir(str(root))
        # Coverage is asked of one member, and attribution needs the whole
        # member list to answer at all -- hence the scope object rather than a
        # bare project dict.
        ok, details = check_coverage(
            entries,
            os.path.join(str(changes_dir.parent), "releases"),
            tag_glob=tag_glob,
            scope=OwnershipScope.for_member(members, proj),
        )
        assert ok, details

    def test_working_tree_clean_after_absorb(self, tmp_path):
        """Absorb self-commits: the working tree is clean afterward."""
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        cmd_absorb(str(root), str(source), "packages/widget",
                   registry_name="widget-npm")

        status = _run_git(str(root), "status", "--porcelain")
        assert status == ""

        proj = next(p for p in load_workspace(str(root)) if p.name == "widget")
        assert proj["path"] == "packages/widget"
        assert proj.registry_name == "widget-npm"

    def test_source_dirty_rejected(self, tmp_path):
        root = _setup_plain_monorepo(tmp_path)
        source, _ = _setup_released_source_repo(tmp_path)
        (source / "dirty.txt").write_text("x\n")
        with pytest.raises(ExtractError, match="uncommitted changes"):
            cmd_absorb(str(root), str(source), "packages/widget")


def _releasable_monorepo_for_absorb(tmp_path, name="core", version="0.0.1"):
    """A committed workspace whose releasable ``core`` has REAL release state.

    ``--releasable`` joins an existing group, and a group with no version file,
    no changes directory and no archives is not a releasable this conversion
    can absorb into -- it is one mid-migration. The version is deliberately
    below the source's, so no version the source carries is already released
    here.
    """
    root = tmp_path / "mono_rel"
    root.mkdir()
    _init_git_repo(root)
    _write_workspace(root, f"""
[[projects]]
path = "existing"
name = "existing"
releasable = "{name}"
""", releasables_toml=f"""
[[releasables]]
name = "{name}"
""")
    (root / "existing").mkdir()
    _make_commit(root, "existing/keep.txt", "keep\n", "add existing")
    make_releasable_state(root, name, version=version)
    subprocess.run(["git", "add", WORKSPACE_DIR], cwd=str(root),
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", "workspace"], cwd=str(root),
                   check=True, capture_output=True, text=True)
    return root


@skip_no_filter_repo
class TestAbsorbReleasable:
    def test_releasable_routing_and_residue_removal(self, tmp_path):
        """--releasable routes the whole release state to the releasable dir.

        REWORKED: the old assertion was that the CHANGELOG was routed and the
        per-package changes directory removed. The conversion now moves the
        release archives too, and the residue it removes is the whole
        per-package release state -- changes/, releases/ and the scaffolding
        version file -- while the member's hooks and config stay.
        """
        root = _releasable_monorepo_for_absorb(tmp_path)
        rel_changes = pathlib.Path(get_releasable_changes_dir(str(root), "core"))

        source, _ = _setup_released_source_repo(tmp_path)
        cmd_absorb(
            str(root), str(source), "packages/widget",
            releasable_name="core",
        )

        # Project assigned to the releasable.
        proj = next(p for p in load_workspace(str(root)) if p.name == "widget")
        assert proj.releasable == "core"

        # Changelog routed into the releasable dir, released files and all.
        entries = parse_jsonl(str(rel_changes / "unreleased.jsonl"))
        assert "Work in progress" in [e.description for e in entries]
        assert (rel_changes / "0.1.0.jsonl").is_file()
        assert (rel_changes / "0.2.0.jsonl").is_file()

        # The release archives came too, and they are locked.
        releases = rel_changes.parent / "releases"
        assert (releases / "v0.1.0.toml").is_file()
        assert oct(os.stat(str(releases / "v0.2.0.toml")).st_mode & 0o777) == "0o444"

        # Per-package residue removed; the member's own config stays.
        member_rlsbl = root / "packages" / "widget" / ".rlsbl"
        assert not (member_rlsbl / "changes").exists()
        assert not (member_rlsbl / "releases").exists()
        assert (member_rlsbl / "config.json").is_file()

        # The existing releasable's version is its own: absorb never bumps it.
        version_file = os.path.join(get_releasable_dir(str(root), "core"), "version")
        assert open(version_file).read().strip() == "0.0.1"

        # Self-committed: clean tree.
        assert _run_git(str(root), "status", "--porcelain") == ""

    def test_a_releasable_with_no_release_state_is_refused(self, tmp_path):
        """FLIPPED: joining a group that has no state is a hard error.

        The old command absorbed into a bare ``[[releasables]]`` entry and
        appended the arriving changelog to a directory nothing else described,
        leaving a releasable with a changelog and no version. The rebuilt one
        refuses and names the migration that creates the state.
        """
        root = tmp_path / "mono_bare"
        root.mkdir()
        _init_git_repo(root)
        _write_workspace(root, """
[[projects]]
path = "existing"
name = "existing"
releasable = "core"
""", releasables_toml="""
[[releasables]]
name = "core"
""")
        (root / "existing").mkdir()
        _make_commit(root, "existing/keep.txt", "keep\n", "add existing")
        subprocess.run(["git", "add", WORKSPACE_DIR], cwd=str(root),
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-q", "-m", "workspace"], cwd=str(root),
                       check=True, capture_output=True, text=True)
        source, _ = _setup_released_source_repo(tmp_path)

        with pytest.raises(AbsorbError) as exc:
            cmd_absorb(
                str(root), str(source), "packages/widget",
                releasable_name="core", dry_run=True,
            )
        message = str(exc.value)
        assert "no release state to absorb into" in message
        assert "rlsbl monorepo migrate-releasable core" in message


class TestAbsorbCliBinding:
    def test_positional_binding_order_locked(self):
        """CLI positional order is source_repo FIRST, dest_path SECOND.

        strictcli binds bottom-decorator-first; this pins the order so a future
        edit cannot silently invert the two positionals.
        """
        from rlsbl import app

        def walk(commands, groups, prefix):
            for name, cmd in commands.items():
                yield prefix + name, cmd
            for gname, group in groups.items():
                yield from walk(group.commands, group._groups, prefix + gname + " ")

        cmd = None
        for path, c in walk(app._commands, app._groups, ""):
            if path == "monorepo absorb":
                cmd = c
        assert cmd is not None
        assert [a.name for a in cmd.args] == ["source_repo", "dest_path"]


# ---------------------------------------------------------------------------
# cmd_extract_releasable (integration tests requiring git-filter-repo)
# ---------------------------------------------------------------------------


def _setup_monorepo_with_releasables(tmp_path):
    """Create a monorepo with explicit releasables.

    Returns (root, commit_hashes).
    """
    root = tmp_path / "monorepo_rel"
    root.mkdir()
    _init_git_repo(root)

    # Create package directories
    (root / "pkgA").mkdir()
    (root / "pkgB").mkdir()
    (root / "pkgC").mkdir()

    # Write workspace.toml with releasables
    _write_workspace(root, """
[[projects]]
path = "pkgA"
name = "pkgA"
releasable = "core"

[[projects]]
path = "pkgB"
name = "pkgB"
releasable = "core"

[[projects]]
path = "pkgC"
name = "pkgC"
releasable = "extras"
""", releasables_toml="""
[[releasables]]
name = "core"

[[releasables]]
name = "extras"
""")

    hash_a = _make_commit(root, "pkgA/main.py", "print('A')", "add pkgA")
    hash_b = _make_commit(root, "pkgB/main.py", "print('B')", "add pkgB")
    hash_c = _make_commit(root, "pkgC/main.py", "print('C')", "add pkgC")

    # Add changelog for pkgA
    changes_a = root / "pkgA" / ".rlsbl" / "changes"
    _write_changelog_entry(changes_a, "unreleased.jsonl", [
        ChangelogEntry(
            commits=[hash_a[:8]],
            user_facing=True,
            description="Feature A",
            type="feature",
            packages=["pkgA"],
        ),
    ])
    _make_commit(
        root, "pkgA/.rlsbl/changes/unreleased.jsonl",
        open(str(changes_a / "unreleased.jsonl")).read(), "changelog A"
    )

    # Commit workspace
    subprocess.run(
        ["git", "add", WORKSPACE_DIR],
        cwd=str(root), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add workspace"],
        cwd=str(root), check=True, capture_output=True, text=True,
    )

    return root, {"pkgA": [hash_a], "pkgB": [hash_b], "pkgC": [hash_c]}


def _setup_monorepo_with_releasable_state(tmp_path):
    """Explicit-mode monorepo whose release state is in the REAL place.

    ``_setup_monorepo_with_releasables`` above declares ``[[releasables]]`` but
    keeps changelog state in per-package ``<pkg>/.rlsbl/changes/`` -- the
    pre-releasable layout. This one puts version, changes/, releases/ and
    config.json under ``.rlsbl-monorepo/releasables/<name>/``, which is where
    the releasable model actually keeps them, and creates NO per-package
    changes dirs at all.

    Layout: releasable ``core`` (members pkgA, pkgB) and releasable ``extras``
    (member pkgC). Each carries the released state for the version the factory
    tags (``ns.initial_version``: locked JSONL, generated .md, archived release
    file) plus the unreleased entries added below.

    Returns (root, commit_hashes).
    """
    root = tmp_path / "monorepo_rel_state"
    ns = make_releasable_monorepo(
        root,
        releasables=[Releasable(name="core"), Releasable(name="extras")],
        projects=[
            {"path": "pkgA", "name": "pkgA", "releasable": "core"},
            {"path": "pkgB", "name": "pkgB", "releasable": "core"},
            {"path": "pkgC", "name": "pkgC", "releasable": "extras"},
        ],
    )

    hash_a = _make_commit(root, "pkgA/main.py", "print('A')", "add pkgA")
    hash_b = _make_commit(root, "pkgB/main.py", "print('B')", "add pkgB")
    hash_c = _make_commit(root, "pkgC/main.py", "print('C')", "add pkgC")

    # Unreleased entries referencing the commits above. The released version
    # (``ns.initial_version``, the one the factory tags) already has its full
    # trio, so this pass only adds the unreleased side -- naming that version
    # again would be refused as a rewrite of released state.
    make_releasable_state(
        root,
        "core",
        version=ns.initial_version,
        unreleased_entries=[
            ChangelogEntry(
                commits=[hash_a], user_facing=True,
                description="Feature A", type="feature", packages=["pkgA"],
            ),
            ChangelogEntry(
                commits=[hash_b], user_facing=True,
                description="Feature B", type="feature", packages=["pkgB"],
            ),
        ],
        release_file=DEFAULT_RELEASE_FILE,
    )
    make_releasable_state(
        root,
        "extras",
        version=ns.initial_version,
        unreleased_entries=[
            ChangelogEntry(
                commits=[hash_c], user_facing=True,
                description="Feature C", type="feature", packages=["pkgC"],
            ),
        ],
    )

    subprocess.run(
        ["git", "add", WORKSPACE_DIR],
        cwd=str(root), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "releasable state"],
        cwd=str(root), check=True, capture_output=True, text=True,
    )

    return root, {"pkgA": [hash_a], "pkgB": [hash_b], "pkgC": [hash_c]}


@skip_no_filter_repo
class TestExtractOnTheReleasableStateLayout:
    """The extract cases on the REAL releasable-state layout.

    State under ``.rlsbl-monorepo/releasables/<name>/``, no per-package
    ``.rlsbl/changes/`` -- which is where the releasable model actually keeps a
    releasable's version, changelog and release archives.

    These used to pin a gap: the old implementation read per-package
    ``.rlsbl/changes/`` only, so on this layout it migrated NOTHING, the
    extracted repository carried no release state at all, and the source was
    left holding an orphaned state directory for a releasable it no longer
    declared. Each of those assertions is now its opposite -- the state is
    transplanted whole and the source's copy is removed -- which is the point
    of the rebuild.
    """

    def test_dry_run_reports_the_plan_without_modifying_anything(self, tmp_path):
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted_rel"

        preview = cmd_extract(str(root), "core", str(target), dry_run=True)

        item = preview.by_key("releasable")
        assert item.state == "extract_to_workspace"
        assert "pkgA" in "\n".join(item.facts)
        assert "pkgB" in "\n".join(item.facts)
        assert not target.exists()

    def test_multi_member_creates_monorepo(self, tmp_path):
        """Extracting a multi-member releasable creates a new monorepo whose
        workspace.toml keeps the releasable grouping."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted_rel"

        cmd_extract(str(root), "core", str(target))

        ws_file = target / WORKSPACE_DIR / WORKSPACE_FILE
        assert ws_file.is_file()

        new_projects = load_workspace(str(target))
        names = [p.name for p in new_projects]
        assert "pkgA" in names
        assert "pkgB" in names
        assert [r.name for r in load_releasables(str(target), new_projects)] == ["core"]

        # Member files survived the filter.
        assert (target / "pkgA" / "main.py").is_file()
        assert (target / "pkgB" / "main.py").is_file()

    def test_the_releasable_state_is_transplanted_whole(self, tmp_path):
        """FLIPPED: the releasable's own state directory moves with it.

        The old behavior migrated zero entries here, because it looked only in
        per-package ``.rlsbl/changes/`` directories this layout does not have.
        """
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted_rel"

        cmd_extract(str(root), "core", str(target))

        state = target / WORKSPACE_DIR / "releasables" / "core"
        assert state.is_dir()
        assert (state / "version").is_file()
        assert (state / "config.json").is_file()
        assert (state / "changes" / "unreleased.jsonl").is_file()
        assert (state / "changes" / "0.1.0.jsonl").is_file()
        assert (state / "releases" / "v0.1.0.toml").is_file()
        # The entries came with it: pkgA's and pkgB's unreleased work.
        entries = parse_jsonl(str(state / "changes" / "unreleased.jsonl"))
        assert {e.description for e in entries} == {"Feature A", "Feature B"}
        # And no per-package changes directory is invented for either member.
        assert not (target / "pkgA" / ".rlsbl" / "changes").exists()
        assert not (target / "pkgB" / ".rlsbl" / "changes").exists()

    def test_source_releasable_state_is_removed(self, tmp_path):
        """FLIPPED: the source no longer keeps an orphaned state directory.

        It used to survive in the source with its changelog intact, describing a
        releasable workspace.toml no longer declared.
        """
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted_rel"

        cmd_extract(str(root), "core", str(target))

        projects = load_workspace(str(root))
        names = [p.name for p in projects]
        assert "pkgA" not in names
        assert "pkgB" not in names
        assert "pkgC" in names
        assert [r.name for r in load_releasables(str(root), projects)] == ["extras"]

        assert not os.path.isdir(get_releasable_dir(str(root), "core"))
        assert not os.path.exists(
            os.path.join(get_releasable_changes_dir(str(root), "core"),
                         "unreleased.jsonl")
        )
        # The member directories went with it, and the edit is committed.
        assert not (root / "pkgA").exists()
        assert not (root / "pkgB").exists()
        assert _run_git(str(root), "status", "--porcelain") == ""

    def test_single_member_creates_flat_repo_with_its_state(self, tmp_path):
        """FLIPPED: the flat repo carries the releasable's changelog too.

        It used to get a synthesized ``.rlsbl/config.json`` and nothing else --
        no ``changes/`` directory at all on this layout.
        """
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        target = tmp_path / "extracted_extras"

        cmd_extract(str(root), "extras", str(target))

        assert os.path.isfile(str(target / "main.py"))
        assert os.path.isfile(str(target / "pyproject.toml"))
        assert os.path.isfile(str(target / ".rlsbl" / "config.json"))
        assert (target / ".rlsbl" / "changes" / "unreleased.jsonl").is_file()
        assert (target / ".rlsbl" / "releases" / "v0.1.0.toml").is_file()
        entries = parse_jsonl(
            str(target / ".rlsbl" / "changes" / "unreleased.jsonl")
        )
        assert [e.description for e in entries] == ["Feature C"]

    def test_tags_are_kept_for_multi_member(self, tmp_path):
        """The releasable-scheme tag is kept and foreign tags pruned."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        _git_tag(root, "extras@v0.3.0")  # foreign -- must be pruned

        target = tmp_path / "extracted_rel"
        cmd_extract(str(root), "core", str(target))

        tags = _run_git(str(target), "tag", "-l").split()
        # make_releasable_monorepo tags each releasable at its initial version.
        assert "core@v0.1.0" in tags
        assert "extras@v0.1.0" not in tags
        assert "extras@v0.3.0" not in tags
        assert _run_git(str(target), "status", "--porcelain") == ""


def _absorb_into_releasable(tmp_path, name="widget"):
    """Absorb a released standalone repo as the sole member of a releasable.

    REWORKED: the releasable is no longer declared up front and its version is
    no longer written by hand afterwards. An absorb with no ``--releasable``
    CREATES the singleton releasable for the arriving member, with the version
    the source declares and its tag_format written explicitly -- so what comes
    back out of the extract is a unit the absorb fully described.
    Returns ``(root, source)``.
    """
    root = tmp_path / "mono"
    root.mkdir()
    _init_git_repo(root)
    (root / "existing").mkdir()
    _write_workspace(root, """
[[projects]]
path = "existing"
name = "existing"
releasable = false
""")
    _make_commit(root, "existing/keep.txt", "keep\n", "add existing")
    subprocess.run(["git", "add", WORKSPACE_DIR], cwd=str(root),
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", "workspace"], cwd=str(root),
                   check=True, capture_output=True, text=True)

    source, _ = _setup_released_source_repo(tmp_path)
    cmd_absorb(str(root), str(source), f"packages/{name}")
    return root, source


@skip_no_filter_repo
class TestExtractRoundTrip:
    """Absorb a released repo into a monorepo, then extract it back out.

    The round trip is the coherence test: the standalone tags come back, the
    changelog hashes resolve in the new object graph, coverage passes, and the
    working tree is committed and clean. Reworked from the package-level form:
    the repo is absorbed as the sole member of a releasable, which is the unit
    the extract now takes back out.
    """

    def test_absorb_then_extract_is_coherent(self, tmp_path):
        root, _ = _absorb_into_releasable(tmp_path)

        out = tmp_path / "widget_out"
        preview = cmd_extract(str(root), "widget", str(out))
        assert preview.by_key("releasable").state == "extract_to_standalone"

        # 1. Standalone anchor tags restored at the right commits.
        tags = _run_git(str(out), "tag", "-l").split()
        assert "v0.1.0" in tags
        assert "v0.2.0" in tags

        # 2. The only monorepo-scheme tag left is the boundary alias at the
        #    current version, which is deliberate: a consumer that knows the
        #    pre-conversion name still resolves it.
        assert "widget@v0.1.0" not in tags
        assert "widget@v0.2.0" in tags
        assert _run_git(str(out), "rev-list", "-n", "1", "widget@v0.2.0") == (
            _run_git(str(out), "rev-list", "-n", "1", "v0.2.0")
        )

        # 3. The v0.2.0 anchor is the newest reachable tag.
        desc = _run_git(str(out), "describe", "--tags", "--match", "v*")
        assert desc.startswith("v0.2.0")

        # 4. Every surviving JSONL hash resolves in the extracted repo.
        changes_dir = out / ".rlsbl" / "changes"
        all_hashes = []
        for jf in changes_dir.glob("*.jsonl"):
            for entry in _parse_jsonl_hashes(str(jf)):
                all_hashes.extend(entry.commits)
        assert all_hashes  # sanity: there are hashes to check
        for h in all_hashes:
            _run_git(str(out), "cat-file", "-e", h + "^{commit}")

        # 5. The RELEASE ANCHORS survive both directions. Each archive names a
        #    commit and the tree of every path that version shipped; absorb
        #    remapped both onto the rewritten monorepo history and re-keyed the
        #    paths to the member's, and the extract mapped them back to a
        #    repository root. FLIPPED: this test used to fabricate a ledger
        #    entry here, because absorb imported only tags.
        from rlsbl.release_file import read_release_file

        releases_dir = changes_dir.parent / "releases"
        for version in ("0.1.0", "0.2.0"):
            archive = releases_dir / f"v{version}.toml"
            assert archive.is_file()
            anchor = read_release_file(str(archive))
            _run_git(str(out), "cat-file", "-e", anchor.candidate_sha + "^{commit}")
            # Back at the repository root, and the recorded tree is the one the
            # commit really has there -- content-identical through two
            # rewrites, since neither changed a byte of the released content.
            assert list(anchor.tree_hashes) == ["."]
            assert anchor.tree_hashes["."] == _run_git(
                str(out), "rev-parse", f"{anchor.candidate_sha}^{{tree}}",
            )
            # The tag and the ledger still agree about that version.
            assert _run_git(str(out), "rev-list", "-n", "1", f"v{version}") == (
                anchor.candidate_sha
            )

        # 6. Changelog coverage passes in the extracted repo, over the ledger
        #    the conversions carried rather than one the test wrote.
        from rlsbl.changelog.validate import check_coverage

        entries = _parse_jsonl_hashes(str(changes_dir / "unreleased.jsonl"))
        prev_cwd = os.getcwd()
        os.chdir(str(out))
        try:
            ok, details = check_coverage(
                entries, os.path.join(str(changes_dir.parent), "releases"),
                tag_glob="v*",
            )
        finally:
            os.chdir(prev_cwd)
        assert ok, details

        # 7. The extracted repo self-committed: clean working tree.
        assert _run_git(str(out), "status", "--porcelain") == ""

        # 8. Source monorepo no longer lists the extracted package.
        assert "widget" not in [p.name for p in load_workspace(str(root))]

    def test_extract_translation_collision_is_hard_error(self, tmp_path):
        """A pre-existing standalone tag colliding with a translated tag aborts
        the extract with a clear error naming both tags.

        REWORKED: the absorb now leaves ``v0.2.0`` itself as the boundary alias
        at the current version, and a tag standing at the very commit the
        translation would produce is not a collision (the next test pins that).
        The collision is a DIFFERENT commit under a name a translation wants,
        which is what ``v0.1.0`` at HEAD is here.
        """
        root, _ = _absorb_into_releasable(tmp_path)
        _git_tag(root, "v0.1.0")

        out = tmp_path / "widget_out"
        with pytest.raises(ExtractError, match="collision"):
            cmd_extract(str(root), "widget", str(out))
        assert not out.exists()

    def test_the_inbound_boundary_alias_is_not_a_collision(self, tmp_path):
        """The round trip's own tag is not something to resolve.

        Absorb keeps the source's ``v0.2.0`` beside ``widget@v0.2.0`` at one
        commit. Extracting translates ``widget@v0.2.0`` back to ``v0.2.0`` --
        the name that is already there, on that same commit. Refusing that
        would make a repository unable to leave the workspace it entered.
        """
        root, _ = _absorb_into_releasable(tmp_path)
        out = tmp_path / "widget_out"

        preview = cmd_extract(str(root), "widget", str(out), dry_run=True)
        assert preview.by_key("tags").state == "translate_tags"

        cmd_extract(str(root), "widget", str(out))
        assert _run_git(str(out), "rev-list", "-n", "1", "v0.2.0") == (
            _run_git(str(out), "rev-list", "-n", "1", "widget@v0.2.0")
        )


@skip_no_filter_repo
class TestExtractOnTheHalfMigratedLayout:
    """A workspace that declares releasables but keeps release state per package.

    ``_setup_monorepo_with_releasables`` is that shape: ``[[releasables]]`` in
    workspace.toml, changelog entries under ``pkgA/.rlsbl/changes/``, and no
    releasable state directories at all. It is a workspace mid-migration, and
    the old command extracted it anyway -- producing a repository with a
    releasable grouping and no version, changelog or release archives behind it.

    The rebuilt conversion moves the releasable's state directory, so it refuses
    a releasable that has none and names the migration that creates it. Every
    structural assertion this class used to make (the new workspace.toml, the
    kept and translated tags, the emptied source) is made in
    ``TestExtractOnTheReleasableStateLayout`` against the layout where the state
    exists.
    """

    def test_a_releasable_with_no_state_is_refused_with_the_migration(
        self, tmp_path,
    ):
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        target = tmp_path / "extracted_rel"

        with pytest.raises(ExtractError) as exc:
            cmd_extract(str(root), "core", str(target), dry_run=True)
        message = str(exc.value)
        assert "no release state to carry over" in message
        assert "rlsbl monorepo migrate-releasable core" in message

    def test_the_refusal_writes_nothing(self, tmp_path):
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        head_before = _run_git(str(root), "rev-parse", "HEAD")
        target = tmp_path / "extracted_rel"

        with pytest.raises(ExtractError):
            cmd_extract(str(root), "core", str(target))

        assert not target.exists()
        assert _run_git(str(root), "rev-parse", "HEAD") == head_before
        assert "pkgA" in [p.name for p in load_workspace(str(root))]
        assert (root / "pkgA" / ".rlsbl" / "changes").is_dir()

    def test_nonexistent_releasable_error(self, tmp_path):
        """Error when the releasable does not exist."""
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        target = tmp_path / "extracted"

        with pytest.raises(ExtractError, match="not found"):
            cmd_extract(str(root), "nonexistent", str(target))

    def test_target_exists_error(self, tmp_path):
        """Error when target path already exists."""
        root, _ = _setup_monorepo_with_releasables(tmp_path)
        target = tmp_path / "extracted"
        target.mkdir()

        with pytest.raises(ExtractError, match="target path already exists"):
            cmd_extract(str(root), "core", str(target))


# ---------------------------------------------------------------------------
# Broken target-declaration guard (no-silent-degradation)
# ---------------------------------------------------------------------------


def _commit_all(repo, message):
    subprocess.run(["git", "add", "-A"], cwd=str(repo),
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(repo),
                   check=True, capture_output=True, text=True)


class TestBrokenTargetDeclarationGuard:
    """A .rlsbl/config.json that exists but has no ``targets`` key is a broken
    declaration -- extract/absorb must hard-error UP FRONT (before any history
    rewrite) rather than silently importing wrongly-schemed tags. A repo with
    NO config file at all is the legitimate auto-detect path and must succeed.
    """

    # --- absorb: validation level (no filter-repo needed) ---

    def test_validate_absorb_broken_source_config_rejected(self, tmp_path, monkeypatch):
        """REWORKED onto the observation that replaced the validator."""
        monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
        root = _absorb_workspace(tmp_path)
        source = _clean_source(tmp_path, name="broken_src")
        os.makedirs(str(source / ".rlsbl"), exist_ok=True)
        (source / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci"}) + "\n"
        )
        _commit_all(source, "add broken config")

        with pytest.raises(AbsorbError, match="broken target declaration"):
            cmd_absorb(
                str(root), str(source), "pkgs/new", name="new_pkg", dry_run=True,
            )

    def test_validate_absorb_no_config_source_ok(self, tmp_path, monkeypatch):
        """A source with NO .rlsbl/config.json auto-detects and passes."""
        monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
        root = _absorb_workspace(tmp_path)
        source = _clean_source(tmp_path)  # no .rlsbl at all

        preview = cmd_absorb(
            str(root), str(source), "pkgs/new", name="new_pkg", dry_run=True,
        )
        assert preview.by_key("source").state == "rewrite_history"
        assert len(declared_members(load_workspace(str(root)))) == 1

    def test_absorb_broken_config_hard_errors_pre_mutation(self, tmp_path, monkeypatch):
        """End-to-end: a broken source config aborts before the monorepo is
        touched (no clone, no merge, no workspace entry)."""
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/git-filter-repo")
        root = _setup_plain_monorepo(tmp_path)
        head_before = _run_git(str(root), "rev-parse", "HEAD")
        source = tmp_path / "broken_src"
        source.mkdir()
        _init_git_repo(source)
        os.makedirs(str(source / ".rlsbl"), exist_ok=True)
        (source / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci"}) + "\n"
        )
        _make_commit(source, "main.py", "print('x')\n", "add main")
        _commit_all(source, "add broken config")

        with pytest.raises(ExtractError, match="broken target declaration"):
            cmd_absorb(str(root), str(source), "packages/widget")

        # Pre-mutation: the monorepo is untouched.
        assert _run_git(str(root), "rev-parse", "HEAD") == head_before
        assert not (root / "packages" / "widget").exists()
        assert "widget" not in [p.name for p in load_workspace(str(root))]

    # --- extract: the guard moved, and what it guards moved with it ---
    #
    # These three go through the conversion itself, which refuses a missing
    # git-filter-repo during observation -- so unlike the absorb cases above,
    # which validate without it, they carry the skip marker.

    @skip_no_filter_repo
    def test_the_extracted_units_own_config_no_longer_reaches_detection(
        self, tmp_path,
    ):
        """A releasable member's tag scheme comes from the releasable.

        The old guard existed because a package-level extract derived the
        extracted package's tag glob from its own detected targets, so a
        ``.rlsbl/config.json`` with no ``targets`` key silently produced the
        wrong scheme. The unit is a releasable now and its scheme is its
        declared ``tag_format``, so a member's broken config cannot mis-scheme
        anything -- and the conversion proceeds.
        """
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        (root / "pkgC" / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci"}) + "\n"
        )
        _commit_all(root, "break pkgC's own target declaration")

        preview = cmd_extract(
            str(root), "extras", str(tmp_path / "out"), dry_run=True,
        )
        assert preview.by_key("releasable").state == "extract_to_standalone"

    @skip_no_filter_repo
    def test_a_remaining_member_with_a_broken_config_is_still_a_hard_error(
        self, tmp_path,
    ):
        """Where the guard still bites: a member OUTSIDE every releasable.

        Its tag glob is derived from its targets, and that glob is what decides
        which tags in the extracted clone are foreign. An undecidable glob would
        prune on a guess, so it is refused before any history is rewritten. The
        repository root is such a member here.
        """
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        root_rlsbl = root / ".rlsbl"
        root_rlsbl.mkdir(exist_ok=True)
        (root_rlsbl / "config.json").write_text(
            json.dumps({"publish_mode": "none"}) + "\n"
        )
        _commit_all(root, "root config without targets")
        target = tmp_path / "extracted"

        with pytest.raises(ExtractError, match="broken target declaration"):
            cmd_extract(str(root), "core", str(target))

        assert not target.exists()
        assert "pkgA" in [p.name for p in load_workspace(str(root))]

    @skip_no_filter_repo
    def test_no_config_at_all_is_still_the_legitimate_auto_detect_path(
        self, tmp_path,
    ):
        """A member with no ``.rlsbl/config.json`` auto-detects and passes."""
        root, _ = _setup_monorepo_with_releasable_state(tmp_path)
        preview = cmd_extract(
            str(root), "core", str(tmp_path / "out"), dry_run=True,
        )
        assert preview.by_key("releasable").state == "extract_to_workspace"
