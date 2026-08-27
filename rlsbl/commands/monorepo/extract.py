"""Absorbing a repository into a monorepo, and the primitives both conversions share.

``cmd_absorb`` is the inbound conversion: an external repository's history is
rewritten to live under a destination path, merged in, its version tags imported
under the monorepo scheme and its changelog hashes remapped onto the new
commits.

The outbound conversion is NOT here. ``rlsbl monorepo extract`` operates on a
whole releasable and is built on the observe/preview/apply skeleton; it lives in
:mod:`rlsbl.commands.monorepo.extract_cmd` and imports the primitives below.
What stays here is what both directions need: the git-filter-repo dependency
check, the git and filter-repo runners, the clone identity fix, and the
dangling-changelog-entry pruning a rewrite leaves behind.
"""

import dataclasses
import os
import shutil
import subprocess
import sys

from ...changelog.files import (
    get_changes_dir,
    list_versioned_files,
    load_filter_repo_commit_map,
    remap_jsonl_hashes,
    writable_jsonl,
)
from ...changelog.schema import parse_jsonl, serialize_entry
from ...errors import RlsblError
from ...tag_glob import TagMode, parse_version_tag
from ...utils import commit_files, is_clean_tree, working_tree_paths
from ...workspace import (
    get_releasable_changes_dir,
    load_workspace,
    save_workspace,
)
from ... import effects


class ExtractError(RlsblError):
    """Error during extract or absorb operations."""


def require_filter_repo():
    """Raise if git-filter-repo is not installed.

    Checks that the ``git-filter-repo`` command is available on PATH.
    Raises ExtractError with install instructions if missing.
    """
    path = shutil.which("git-filter-repo")
    if path is None:
        raise ExtractError(
            "git-filter-repo is not installed. "
            "Install it with: pip install git-filter-repo\n"
            "Or see: https://github.com/newren/git-filter-repo#how-do-i-install-it"
        )
    return path


def _run_git(cwd, *args):
    """Run a git command and return stdout. Raises subprocess.CalledProcessError on failure."""
    result = effects.run(
        ["git"] + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _run_filter_repo(cwd, *args):
    """Run ``git-filter-repo`` in ``cwd``, wrapping failures in ExtractError.

    Raw ``subprocess.CalledProcessError`` from a filter-repo run is an opaque
    stack trace to the caller; wrap it so the extract flow reports a clean,
    actionable error including filter-repo's own stderr.
    """
    try:
        effects.run(
            ["git-filter-repo", *args],
            cwd=str(cwd), check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ExtractError(
            f"git-filter-repo failed (args: {' '.join(args)}):\n"
            f"{exc.stderr or exc.stdout or exc}"
        ) from exc


def _ensure_git_identity(clone_path, source_path):
    """Copy the source repo's committer identity into a fresh clone.

    ``git clone`` does not carry over the source's *local* ``user.name`` /
    ``user.email``, so a commit inside the clone can fail with "please tell me
    who you are" in environments without a global identity. We read the
    source's effective identity (local or global) and set it locally in the
    clone. If the source has none configured, the clone inherits whatever
    global identity exists (unchanged).
    """
    for key in ("user.name", "user.email"):
        try:
            val = _run_git(source_path, "config", "--get", key)
        except subprocess.CalledProcessError:
            continue
        if val:
            _run_git(clone_path, "config", key, val)


def _commit_resolves(repo, commit_hash):
    """Whether ``commit_hash`` resolves to an existing commit object in ``repo``."""
    result = effects.run(
        ["git", "cat-file", "-e", commit_hash + "^{commit}"],
        cwd=str(repo), capture_output=True, text=True,
    )
    return result.returncode == 0


def _prune_dangling_entries(changes_dir, repo_root):
    """Drop changelog entries whose commits no longer resolve after a rewrite.

    Runs AFTER :func:`remap_jsonl_hashes` has mapped every survivable hash to
    its post-rewrite SHA. Any commit that still fails to resolve in
    ``repo_root`` was pruned by the filter (or was never mappable):

    - An entry with at least one surviving commit is kept, narrowed to just the
      resolving hashes (a partial survival, logged).
    - An entry whose EVERY commit fails to resolve is DROPPED entirely, with a
      loud log line -- never left dangling with a null/stale hash.

    Returns the number of entries dropped.
    """
    if not os.path.isdir(changes_dir):
        return 0
    dropped = 0
    for name in sorted(os.listdir(changes_dir)):
        if not name.endswith(".jsonl"):
            continue
        filepath = os.path.join(changes_dir, name)
        entries = parse_jsonl(filepath)
        new_entries = []
        changed = False
        for entry in entries:
            surviving = [h for h in entry.commits if _commit_resolves(repo_root, h)]
            if not surviving:
                changed = True
                dropped += 1
                desc = entry.description or "(non-user-facing)"
                print(
                    f"note: dropping changelog entry '{desc}' from {name} -- "
                    f"all referenced commits were pruned by the extract "
                    f"rewrite",
                    file=sys.stderr,
                )
                continue
            if len(surviving) != len(entry.commits):
                changed = True
                print(
                    f"note: narrowing changelog entry in {name} to surviving "
                    f"commits ({len(surviving)}/{len(entry.commits)}) after "
                    f"extract rewrite",
                    file=sys.stderr,
                )
                new_entries.append(dataclasses.replace(entry, commits=surviving))
            else:
                new_entries.append(entry)
        if not changed:
            continue
        content = "".join(serialize_entry(e) + "\n" for e in new_entries)
        with writable_jsonl(filepath):
            # file_mode pins what the mkstemp-based hand-rolled write left
            # here; writable_jsonl relocks released files on exit regardless.
            effects.atomic_write_text(filepath, content, file_mode=0o600)
    return dropped


def validate_absorb_preconditions(workspace_root, source_repo_path, dest_path, name):
    """Validate that absorption can proceed.

    Checks (mirrors ``monorepo add`` for uniqueness):
    - git-filter-repo is installed (the history rewrite depends on it)
    - Source repo exists and is a git repo
    - Source working tree is CLEAN -- the history filter only captures
      committed state, so uncommitted changes would be silently dropped
    - Destination path is not already registered in workspace.toml
    - Package name is not already registered in workspace.toml

    Returns projects list.
    """
    require_filter_repo()

    if not os.path.isdir(source_repo_path):
        raise ExtractError(
            f"source repo path does not exist: {source_repo_path}"
        )

    if not os.path.isdir(os.path.join(source_repo_path, ".git")):
        raise ExtractError(
            f"source path is not a git repository: {source_repo_path}"
        )

    # One clean-tree probe for the whole tool (rlsbl.utils.is_clean_tree): it
    # spells the ``--no-optional-locks`` form that puts the read on the observe
    # allowlist, so a preview really runs it instead of recording it -- the
    # check below has to DECIDE -- and keeps the read off ``index.lock``.
    if not is_clean_tree(cwd=source_repo_path):
        raise ExtractError(
            f"source repository has uncommitted changes: {source_repo_path}. "
            f"Commit or discard them before absorbing -- the history rewrite "
            f"only captures committed state, so uncommitted changes would be "
            f"silently lost."
        )

    projects = load_workspace(workspace_root)
    norm_dest = dest_path.rstrip("/")
    for proj in projects:
        if proj["path"].rstrip("/") == norm_dest:
            raise ExtractError(
                f"path '{dest_path}' already exists in workspace"
            )
        if proj["name"] == name:
            raise ExtractError(
                f"package '{name}' already exists in workspace"
            )

    _assert_absorb_source_config_valid(source_repo_path)

    return projects


def _assert_absorb_source_config_valid(source_repo_path):
    """Hard-error UP FRONT (before any history rewrite) if the source repo's
    target declaration is broken.

    The source's ``.rlsbl/config.json`` becomes the absorbed package's config,
    so a broken declaration (config file present but no ``targets`` key) would
    otherwise surface only LATER -- after the merge landed -- as a silently
    mis-schemed tag import. A source with NO config file at all is the
    legitimate auto-detect path and passes.

    Raising here replaces the old silent fallback to the ``{name}@v{version}``
    scheme in :func:`_resolve_monorepo_tag_target`.
    """
    from ...errors import ConfigError
    from ...targets import detect_targets

    try:
        detect_targets(source_repo_path)
    except ConfigError as e:
        raise ExtractError(
            f"source repository '{source_repo_path}' has a broken target "
            f"declaration and cannot be absorbed: {e} A repository with a "
            f".rlsbl/config.json must include a \"targets\" key (a repository "
            f"with no .rlsbl/config.json is fine -- targets are auto-detected)."
        ) from e


def _collect_source_tags(source_repo_path):
    """Return ``[(tag_name, commit_sha), ...]`` for every tag in the source repo.

    ``commit_sha`` is the (full) commit the tag resolves to -- annotated tags
    are dereferenced to their target commit via ``git rev-list``.
    """
    out = _run_git(source_repo_path, "tag", "-l")
    tags = []
    for line in out.splitlines():
        tag = line.strip()
        if not tag:
            continue
        sha = _run_git(source_repo_path, "rev-list", "-n", "1", tag)
        tags.append((tag, sha))
    return tags


def _resolve_monorepo_tag_target(workspace_root, dest_path, name):
    """Return a callable ``version -> monorepo tag string`` for the absorbed pkg.

    The tag scheme is resolved from the absorbed package's first detected
    target (Go uses path-style ``{path}/v{version}``; others use
    ``{name}@v{version}``). Uses the default ``{name}@v{version}`` scheme when
    no target is detected (a source with no config at all).

    Assumes the source's target config was validated up front by
    :func:`validate_absorb_preconditions`; a broken declaration is a hard error
    there (before the merge), so this helper never swallows a ``ConfigError``.
    """
    from ...targets import TARGETS, detect_targets

    dest_full = os.path.join(workspace_root, dest_path)
    target = None
    target_entries = detect_targets(dest_full)
    if target_entries and target_entries[0].name in TARGETS:
        target = TARGETS[target_entries[0].name]

    def make_tag(version):
        if target is not None:
            return target.monorepo_tag_format(name, version, path=dest_path)
        return f"{name}@v{version}"

    return make_tag


def cmd_absorb(
    workspace_root, source_repo_path, dest_path, *,
    name=None, registry_name="", releasable_name=None, dry_run=False
):
    """Absorb an external repository as a package in the monorepo.

    Rewrites the source's history to live under ``dest_path`` (rather than a
    verbatim subtree add), preserving its full commit history with rewritten
    paths, importing its version tags under the monorepo tag scheme, and
    remapping its JSONL changelog hashes to the new (post-rewrite) commits.

    Steps:
    1. Validate preconditions (filter-repo present, source clean, uniqueness).
    2. Temp-clone the source and run ``git-filter-repo
       --to-subdirectory-filter <dest_path>`` to relocate all files/history.
    3. Fetch + merge (``--allow-unrelated-histories``) the rewritten history
       into the monorepo -- this is the first commit.
    4. Delete the bare ``v*`` tags the merge auto-followed in, and re-create
       version tags under the monorepo scheme at the mapped commits.
    5. Remap the arriving JSONL changelog hashes to the new commits. In
       releasable mode, move the package's changes into the releasable's
       changes dir and remove the per-package residue via saferm.
    6. Register the project in workspace.toml and commit the follow-up
       (workspace.toml + remap edits + residue removals).

    Args:
        workspace_root: path to the monorepo root.
        source_repo_path: path to the external repository.
        dest_path: directory prefix (and workspace path) for the absorbed pkg.
        name: workspace project name (default: basename of dest_path).
        registry_name: optional registry identity recorded in workspace.toml.
        releasable_name: optional releasable to assign the package to.
        dry_run: if True, validate and report but do not perform the absorption.

    Returns:
        A dict with absorption details.
    """
    workspace_root = os.path.abspath(workspace_root)
    source_repo_path = os.path.abspath(source_repo_path)
    if name is None:
        name = os.path.basename(dest_path.rstrip("/"))

    projects = validate_absorb_preconditions(
        workspace_root, source_repo_path, dest_path, name
    )

    # Classify the source's tags into importable version tags and the rest.
    source_tags = _collect_source_tags(source_repo_path)
    version_tags = []  # (tag_name, old_sha, version)
    skipped_tags = []
    for tag, old_sha in source_tags:
        parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
        if parsed is None:
            skipped_tags.append(tag)
        else:
            version_tags.append((tag, old_sha, parsed.version))

    if dry_run:
        return {
            "name": name,
            "dest_path": dest_path,
            "source_path": source_repo_path,
            "registry_name": registry_name,
            "releasable_name": releasable_name,
            "tags_to_import": [v for _, _, v in version_tags],
            "skipped_tags": skipped_tags,
            "dry_run": True,
        }

    # --- 2. Temp-clone + history rewrite ---
    tmp_root = effects.mkdtemp(prefix="rlsbl-absorb-")
    try:
        clone_path = os.path.join(tmp_root, "clone")
        _run_git(workspace_root, "clone", "--no-local", source_repo_path, clone_path)
        effects.run(
            ["git-filter-repo", "--to-subdirectory-filter", dest_path, "--force"],
            cwd=clone_path, check=True, capture_output=True, text=True,
        )
        commit_map_path = os.path.join(
            clone_path, ".git", "filter-repo", "commit-map"
        )
        sha_map, _pruned = load_filter_repo_commit_map(commit_map_path)

        # --- 3. Fetch + merge the rewritten history (first commit) ---
        # The merge commit is structural (it re-imports already-released
        # history), so it carries the Autogenerated trailer to stay exempt
        # from changelog coverage.
        _run_git(workspace_root, "fetch", clone_path)
        _run_git(
            workspace_root, "merge", "--allow-unrelated-histories",
            "-m", f"monorepo: absorb {name} history",
            "-m", "Autogenerated: true", "FETCH_HEAD",
        )
    finally:
        effects.rmtree(tmp_root, ignore_errors=True)

    # --- 4. Tag hygiene: delete fetched bare tags, import monorepo tags ---
    for tag, _old_sha in source_tags:
        if _run_git(workspace_root, "tag", "-l", tag):
            _run_git(workspace_root, "tag", "-d", tag)

    make_tag = _resolve_monorepo_tag_target(workspace_root, dest_path, name)
    tags_imported = []
    for tag, old_sha, version in version_tags:
        new_sha = sha_map.get(old_sha)
        if new_sha is None:
            # The tagged commit was pruned by the filter -- cannot place it.
            continue
        mono_tag = make_tag(version)
        _run_git(workspace_root, "tag", mono_tag, new_sha)
        tags_imported.append(mono_tag)

    # --- 5. Register project + route/remap changelog ---
    new_project = {"path": dest_path, "name": name}
    if registry_name:
        new_project["registry_name"] = registry_name
    if releasable_name is not None:
        new_project["releasable"] = releasable_name

    from ...workspace import WorkspaceProject
    projects.append(WorkspaceProject(new_project))
    save_workspace(workspace_root, projects)

    dest_full = os.path.join(workspace_root, dest_path)
    pkg_changes_dir = get_changes_dir(dest_full)

    if releasable_name is not None:
        # Route the absorbed package's changelog into the releasable's changes
        # dir (remapped), then remove the per-package residue via saferm.
        rel_changes_dir = get_releasable_changes_dir(workspace_root, releasable_name)
        entries_migrated = _merge_changes_into_releasable(
            pkg_changes_dir, rel_changes_dir
        )
        remap_jsonl_hashes(rel_changes_dir, sha_map)
        from ...releasable_cleanup import cleanup_per_package_release_state
        cleanup_per_package_release_state(workspace_root, projects=projects)
    else:
        entries_migrated = _count_jsonl_entries(pkg_changes_dir)
        remap_jsonl_hashes(pkg_changes_dir, sha_map)

    # --- 6. Follow-up commit: workspace.toml + remap edits + residue ---
    _commit_absorb_followup(workspace_root, name)

    return {
        "name": name,
        "dest_path": dest_path,
        "source_path": source_repo_path,
        "registry_name": registry_name,
        "releasable_name": releasable_name,
        "entries_migrated": entries_migrated,
        "tags_imported": tags_imported,
        "skipped_tags": skipped_tags,
    }


def _count_jsonl_entries(changes_dir):
    """Total number of changelog entries across all JSONL files in a dir."""
    if not os.path.isdir(changes_dir):
        return 0
    total = 0
    unreleased = os.path.join(changes_dir, "unreleased.jsonl")
    if os.path.isfile(unreleased):
        total += len(parse_jsonl(unreleased))
    for _version, path in list_versioned_files(changes_dir):
        total += len(parse_jsonl(path))
    return total


def _merge_changes_into_releasable(pkg_changes_dir, rel_changes_dir):
    """Move a package's arriving JSONL changes into a releasable's changes dir.

    Unreleased entries are appended to the releasable's unreleased.jsonl.
    Versioned files are copied over (skipped if a same-version file already
    exists in the releasable dir, which would indicate a real collision the
    operator must resolve). Returns the number of entries moved.
    """
    if not os.path.isdir(pkg_changes_dir):
        return 0
    effects.makedirs(rel_changes_dir, exist_ok=True)
    moved = 0

    pkg_unreleased = os.path.join(pkg_changes_dir, "unreleased.jsonl")
    if os.path.isfile(pkg_unreleased):
        entries = parse_jsonl(pkg_unreleased)
        if entries:
            rel_unreleased = os.path.join(rel_changes_dir, "unreleased.jsonl")
            with effects.open_write(rel_unreleased, "a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(serialize_entry(entry) + "\n")
            moved += len(entries)

    for version, path in list_versioned_files(pkg_changes_dir):
        target = os.path.join(rel_changes_dir, f"{version}.jsonl")
        if os.path.isfile(target):
            continue
        entries = parse_jsonl(path)
        with effects.open_write(target, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(serialize_entry(entry) + "\n")
        moved += len(entries)

    return moved


def _commit_absorb_followup(workspace_root, name):
    """Commit every working-tree change left after the absorb merge.

    Captures the workspace.toml entry, remapped JSONL edits, moved changelog
    files, and saferm'd residue deletions in a single follow-up commit. Uses
    the Autogenerated trailer so the structural commit is exempt from
    changelog coverage checks.
    """
    paths = working_tree_paths(cwd=str(workspace_root))
    if not paths:
        return
    commit_files(
        f"monorepo: absorb {name}",
        paths,
        cwd=str(workspace_root),
    )


