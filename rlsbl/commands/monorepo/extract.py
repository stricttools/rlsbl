"""Extract and absorb operations for moving packages in and out of monorepos, including history migration, changelog transfer, and workspace.toml updates.

Provides:
- require_filter_repo(): dependency check for git-filter-repo
- cmd_extract(): extract a package from a monorepo into a new repository
- cmd_absorb(): absorb an external repository as a package in the monorepo
- cmd_extract_releasable(): extract all member packages of a releasable
"""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile

from ...changelog.files import (
    get_changes_dir,
    read_unreleased,
    list_versioned_files,
    load_filter_repo_commit_map,
    remap_jsonl_hashes,
    writable_jsonl,
)
from ...changelog.schema import parse_jsonl, serialize_entry
from ...errors import RlsblError
from ...tag_glob import (
    TagMode,
    parse_version_tag,
    releasable_tag_glob,
    resolve_monorepo_tag_glob,
)
from ...utils import commit_files
from ...workspace import (
    get_releasable_changes_dir,
    is_explicit_mode,
    load_workspace,
    load_releasables,
    members_of,
    resolve_releasable_for_project,
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


def _git_tag_list(repo, pattern=None):
    """Return the list of tags in ``repo``, optionally filtered by a glob."""
    args = ["tag", "-l"]
    if pattern is not None:
        args.append(pattern)
    out = _run_git(repo, *args)
    return [t for t in out.splitlines() if t.strip()]


def _commit_resolves(repo, commit_hash):
    """Whether ``commit_hash`` resolves to an existing commit object in ``repo``."""
    result = effects.run(
        ["git", "cat-file", "-e", commit_hash + "^{commit}"],
        cwd=str(repo), capture_output=True, text=True,
    )
    return result.returncode == 0


def _translate_extract_tags(repo, own_glob, foreign_globs, *, keep_own):
    """Translate/prune tags in a freshly extracted repo.

    A clone (then filter-repo) carries EVERY tag the monorepo had: the
    extracted package's own monorepo-scheme tags, foreign packages' scheme
    tags, and any pre-existing standalone ``v*`` tags -- all pointing at
    rewritten commits.

    - Tags matching ``own_glob`` are the extracted package's own tags. When
      ``keep_own`` is False they are retagged as ``v{version}`` at the same
      commit and the monorepo-scheme original is deleted; when True (a
      multi-member releasable, whose releasable scheme stays valid) they are
      left untouched.
    - A tag matching one of ``foreign_globs`` (another CURRENT workspace
      member's/releasable's resolved glob) is a genuine foreign artifact and is
      deleted.
    - A tag that parses as a monorepo/path-scheme version tag but matches NO
      current member glob is KEPT with a log line. Such a tag is most likely
      this package's OWN historical release under an old prefix (e.g. after a
      releasable rename, ``oldname@vX``). Deleting it would destroy the
      package's own release history, so the conservative rule keeps it.
    - Tags that do not parse as a version tag under any scheme (and standalone
      ``v*`` tags that are not translation targets) are left in place, with a
      log line for genuinely unrecognized tags.

    Collision: when ``keep_own`` is False and a translated ``v{version}`` name
    already exists (a pre-existing standalone tag), raise ExtractError naming
    both tags -- never silently clobber.

    Returns ``(translated_or_kept, deleted, left)`` tag-name lists.
    """
    all_tags = set(_git_tag_list(repo))
    own_tags = _git_tag_list(repo, own_glob)
    own_set = set(own_tags)

    # Resolve the concrete set of tags that belong to another live member /
    # releasable (the only tags safe to prune). A scheme-parsing tag NOT in
    # this set is kept, never deleted.
    foreign_tags = set()
    for glob in foreign_globs:
        foreign_tags.update(_git_tag_list(repo, glob))

    # Resolve each own tag to (old_tag, version, sha).
    translations = []
    for tag in own_tags:
        parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
        if parsed is None:
            # Glob matched a non-version tag -- leave it alone.
            continue
        sha = _run_git(repo, "rev-list", "-n", "1", tag)
        translations.append((tag, parsed.version, sha))

    # Collision pre-check (before mutating anything) so we fail cleanly.
    if not keep_own:
        for old_tag, version, _sha in translations:
            new_tag = f"v{version}"
            if new_tag in all_tags and new_tag not in own_set:
                raise ExtractError(
                    f"tag translation collision: cannot rename '{old_tag}' to "
                    f"'{new_tag}' because a tag named '{new_tag}' already "
                    f"exists in the extracted repo. Resolve the conflicting "
                    f"tag before extracting."
                )

    # Prune foreign scheme tags; leave unrecognized / standalone tags.
    deleted = []
    left = []
    for tag in sorted(all_tags):
        if tag in own_set:
            continue
        parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
        if parsed is None:
            left.append(tag)
            print(
                f"note: leaving unrecognized tag '{tag}' in extracted repo "
                f"(matches no member tag scheme)",
                file=sys.stderr,
            )
        elif parsed.scheme == "standalone":
            # A pre-existing standalone tag -- valid in the extracted repo.
            left.append(tag)
        elif tag in foreign_tags:
            # Matches another live member's/releasable's glob -- genuinely
            # foreign, safe to prune.
            _run_git(repo, "tag", "-d", tag)
            deleted.append(tag)
        else:
            # Parses as a scheme tag but matches NO current member glob.
            # Most likely this package's own pre-rename history under an old
            # prefix -- keep it, never destroy release history.
            left.append(tag)
            print(
                f"note: keeping unrecognized scheme tag '{tag}' in extracted "
                f"repo -- possibly pre-rename history (matches no current "
                f"member glob)",
                file=sys.stderr,
            )

    # Apply translations (or keep own tags for multi-member releasables).
    if keep_own:
        return own_tags, deleted, left

    translated = []
    for old_tag, version, sha in translations:
        new_tag = f"v{version}"
        _run_git(repo, "tag", new_tag, sha)
        _run_git(repo, "tag", "-d", old_tag)
        translated.append(new_tag)
    return translated, deleted, left


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
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(filepath), suffix=".tmp"
            )
            try:
                os.write(fd, content.encode("utf-8"))
                os.close(fd)
                os.replace(tmp_path, filepath)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
    return dropped


def _remap_and_prune(changes_dir, sha_map, repo_root):
    """Remap migrated JSONL hashes to post-rewrite SHAs and drop dangling entries.

    Combines :func:`remap_jsonl_hashes` (map old monorepo SHAs to the extracted
    repo's new SHAs) with :func:`_prune_dangling_entries` (drop entries whose
    commits were pruned). Used by both extract commands so remap + prune stay
    identical across them.
    """
    if not os.path.isdir(changes_dir):
        return
    remap_jsonl_hashes(changes_dir, sha_map)
    _prune_dangling_entries(changes_dir, repo_root)


def _commit_extracted_state(repo):
    """Commit the migrated/remapped/retagged .rlsbl state in an extracted repo.

    The extracted repo is a fresh, non-shared clone, so a plain committed
    snapshot is safe. Carries the Autogenerated trailer so the structural
    migration commit is exempt from changelog coverage checks in the new repo.
    Captures every working-tree change (workspace.toml/config edits, remapped
    JSONL, migrated changelog files). No-op when the tree is already clean.
    """
    result = effects.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip().strip('"')
        if rest:
            paths.append(rest)
    if not paths:
        return
    commit_files(
        "chore: migrate rlsbl state from monorepo extract",
        paths,
        cwd=str(repo),
    )


def _resolve_own_tag_glob(workspace_root, project, projects):
    """Resolve the extracted package's own monorepo tag glob.

    Uses the project's releasable ``tag_format`` when the workspace is in
    explicit mode and the project belongs to a releasable; otherwise the
    project's first detected target's monorepo glob.

    Assumes the package's target config was already validated up front by
    :func:`validate_extract_preconditions`. A broken target declaration is a
    hard error THERE (before any history rewrite), never a silent fallback
    here -- so this helper never has to swallow a ``ConfigError``.
    """
    releasables = []
    if is_explicit_mode(workspace_root):
        releasables = load_releasables(workspace_root, projects)
    rel = resolve_releasable_for_project(project, releasables)
    return resolve_monorepo_tag_glob(project, workspace_root, releasable=rel)


def _other_member_globs(
    workspace_root, projects, releasables,
    *, exclude_project_names, exclude_releasable_names=frozenset(),
):
    """Resolve the tag globs of every workspace member/releasable NOT extracted.

    Returns a set of ``git tag`` globs (e.g. ``{"pkgB@v*", "core@v*"}``) covering
    every OTHER live member and releasable in the workspace. These globs identify
    which scheme-parsing tags in a freshly extracted repo are genuinely foreign
    (another live member's release history -- safe to prune) versus orphan tags
    matching no current member. An orphan is most likely the extracted package's
    OWN pre-rename history under an old prefix, which must be kept.

    Must be called against the SOURCE workspace BEFORE the extracted project is
    removed from workspace.toml (target detection reads the still-present member
    dirs).
    """
    globs = set()
    for proj in projects:
        if proj.name in exclude_project_names:
            continue
        rel = resolve_releasable_for_project(proj, releasables)
        globs.add(resolve_monorepo_tag_glob(proj, workspace_root, releasable=rel))
    for rel in releasables:
        if rel.name in exclude_releasable_names:
            continue
        globs.add(releasable_tag_glob(rel.tag_format, rel.name))
    return globs


def _assert_extract_target_config_valid(workspace_root, project, projects):
    """Hard-error UP FRONT if the package's target declaration is broken.

    A *broken* declaration is a ``.rlsbl/config.json`` that exists but has no
    ``targets`` key (``detect_targets`` raises ``ConfigError``). A package with
    NO config file at all is fine -- targets auto-detect. Releasable members
    derive their tag scheme from the releasable ``tag_format`` and skip target
    detection entirely, so they are never broken here.

    Raising here (in the precondition gate, before the clone/filter-repo runs)
    is what replaces the old silent fallback to the ``{name}@v*`` scheme.
    """
    from ...errors import ConfigError
    from ...targets import detect_targets, resolve_releasable_config_dir

    releasables = []
    if is_explicit_mode(workspace_root):
        releasables = load_releasables(workspace_root, projects)
    if resolve_releasable_for_project(project, releasables) is not None:
        return
    rel_dir = resolve_releasable_config_dir(project, workspace_root)
    proj_dir = os.path.join(str(workspace_root), project["path"])
    try:
        detect_targets(proj_dir, releasable_config_dir=rel_dir)
    except ConfigError as e:
        cfg_rel = os.path.join(project["path"], ".rlsbl", "config.json")
        raise ExtractError(
            f"package '{project['name']}' has a broken target declaration and "
            f"cannot be extracted: {e} Add a \"targets\" key to {cfg_rel} (a "
            f"package with no .rlsbl/config.json is fine -- targets are "
            f"auto-detected)."
        ) from e


def _find_project(projects, package_name):
    """Find a project by name in the workspace project list.

    Returns the WorkspaceProject or raises ExtractError.
    """
    for proj in projects:
        if proj.name == package_name:
            return proj
    available = ", ".join(p.name for p in projects)
    raise ExtractError(
        f"package '{package_name}' not found in workspace. Available: {available}"
    )


def _get_default_branch(cwd):
    """Detect the default branch name (main or master) of a repo."""
    try:
        branch = _run_git(cwd, "symbolic-ref", "--short", "HEAD")
        return branch
    except subprocess.CalledProcessError:
        return "main"


def _filter_changelog_entries(entries, package_path, repo_root):
    """Filter changelog entries to those touching a specific package path.

    An entry matches if:
    - It has a ``packages`` field containing the package name, OR
    - Any of its commits touch files under the package path (checked via
      git diff-tree if repo_root is provided and the commit exists).

    When we cannot determine relevance (e.g. no packages field and commits
    are not resolvable), the entry is included (conservative approach).
    """
    pkg_dir = package_path.rstrip("/") + "/"
    filtered = []

    for entry in entries:
        # If the entry has an explicit packages field, use it
        if entry.packages is not None:
            pkg_base = os.path.basename(package_path.rstrip("/"))
            if pkg_base in entry.packages or package_path in entry.packages:
                filtered.append(entry)
            continue

        # Try to determine from commit paths
        if repo_root:
            touches_package = False
            for commit_hash in entry.commits:
                try:
                    files_output = _run_git(
                        repo_root, "diff-tree", "--no-commit-id", "-r",
                        "--name-only", commit_hash
                    )
                    for fpath in files_output.splitlines():
                        if fpath.startswith(pkg_dir) or fpath == package_path:
                            touches_package = True
                            break
                except subprocess.CalledProcessError:
                    # Commit not resolvable -- include conservatively
                    touches_package = True
                    break
                if touches_package:
                    break
            if touches_package:
                filtered.append(entry)
        else:
            # No repo root -- include conservatively
            filtered.append(entry)

    return filtered


def _migrate_changelog_to_new_repo(
    source_changes_dir, target_changes_dir, package_path, repo_root
):
    """Migrate changelog entries relevant to a package into a new repo's changes dir.

    Reads unreleased.jsonl and all versioned JSONL files from source_changes_dir,
    filters entries to those relevant to the package, and writes them into
    target_changes_dir.

    Returns (files_written, entries_migrated) tuple.
    """
    os.makedirs(target_changes_dir, exist_ok=True)
    files_written = 0
    entries_migrated = 0

    # Migrate unreleased entries
    unreleased_entries = read_unreleased(os.path.dirname(source_changes_dir))
    if unreleased_entries:
        # read_unreleased expects the project path, not changes_dir directly
        # Re-read from the actual changes dir
        unreleased_path = os.path.join(source_changes_dir, "unreleased.jsonl")
        if os.path.isfile(unreleased_path):
            unreleased_entries = parse_jsonl(unreleased_path)
    else:
        unreleased_path = os.path.join(source_changes_dir, "unreleased.jsonl")
        if os.path.isfile(unreleased_path):
            unreleased_entries = parse_jsonl(unreleased_path)

    if unreleased_entries:
        filtered = _filter_changelog_entries(unreleased_entries, package_path, repo_root)
        if filtered:
            target_unreleased = os.path.join(target_changes_dir, "unreleased.jsonl")
            with open(target_unreleased, "w", encoding="utf-8") as f:
                for entry in filtered:
                    f.write(serialize_entry(entry) + "\n")
            files_written += 1
            entries_migrated += len(filtered)

    # Migrate versioned entries
    versioned = list_versioned_files(source_changes_dir)
    for version_str, version_path in versioned:
        version_entries = parse_jsonl(version_path)
        filtered = _filter_changelog_entries(version_entries, package_path, repo_root)
        if filtered:
            target_version = os.path.join(target_changes_dir, f"{version_str}.jsonl")
            with open(target_version, "w", encoding="utf-8") as f:
                for entry in filtered:
                    f.write(serialize_entry(entry) + "\n")
            files_written += 1
            entries_migrated += len(filtered)

    # Ensure unreleased.jsonl exists even if empty
    target_unreleased = os.path.join(target_changes_dir, "unreleased.jsonl")
    if not os.path.isfile(target_unreleased):
        with open(target_unreleased, "w", encoding="utf-8") as f:
            pass
        files_written += 1

    return files_written, entries_migrated


def _create_rlsbl_config(target_path, source_config_path=None):
    """Create a .rlsbl/ config in the target repo.

    If source_config_path is provided and exists, copies relevant config.
    Otherwise creates a minimal config.
    """
    rlsbl_dir = os.path.join(target_path, ".rlsbl")
    os.makedirs(rlsbl_dir, exist_ok=True)

    config = {}
    if source_config_path and os.path.isfile(source_config_path):
        with open(source_config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

    # Ensure publish_mode is set (default: publish via CI)
    if "publish_mode" not in config:
        config["publish_mode"] = "ci"

    config_path = os.path.join(rlsbl_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    return config_path


def _remove_project_from_workspace(workspace_root, package_name, projects):
    """Remove a project from workspace.toml by name.

    Returns the updated project list.
    """
    updated = [p for p in projects if p.name != package_name]
    if len(updated) == len(projects):
        raise ExtractError(
            f"package '{package_name}' not found in workspace projects"
        )
    save_workspace(workspace_root, updated)
    return updated


def validate_extract_preconditions(workspace_root, package_name, target_repo_path):
    """Validate that extraction can proceed.

    Checks:
    - Package exists in workspace.toml
    - Target path does not already exist
    - git-filter-repo is installed

    Returns (projects, project) tuple.
    """
    require_filter_repo()

    projects = load_workspace(workspace_root)
    project = _find_project(projects, package_name)

    if os.path.exists(target_repo_path):
        raise ExtractError(
            f"target path already exists: {target_repo_path}"
        )

    _assert_extract_target_config_valid(workspace_root, project, projects)

    return projects, project


def cmd_extract(workspace_root, package_name, target_repo_path, *, dry_run=False):
    """Extract a package from the monorepo into a new repository.

    Steps:
    1. Validate package exists in workspace.toml
    2. Clone the monorepo to target_repo_path
    3. Run ``git filter-repo --path <pkg-dir>`` on the clone to keep only
       that package's history
    4. Migrate changelog: filter JSONL entries to those touching the
       extracted package
    5. Create ``.rlsbl/`` config in the new repo
    6. Update source monorepo: remove project from workspace.toml

    Args:
        workspace_root: path to the monorepo root.
        package_name: name of the package to extract.
        target_repo_path: path where the new repo will be created.
        dry_run: if True, validate but do not perform the extraction.

    Returns:
        A dict with extraction details: package_name, target_path,
        entries_migrated, files_written.
    """
    projects, project = validate_extract_preconditions(
        workspace_root, package_name, target_repo_path
    )
    package_path = project.path

    if dry_run:
        return {
            "package_name": package_name,
            "target_path": target_repo_path,
            "package_path": package_path,
            "dry_run": True,
        }

    # Resolve the extracted package's own tag glob AND the other live members'
    # globs BEFORE mutating the source (target detection reads the still-present
    # package dirs). The foreign globs decide which scheme tags are safe to
    # prune; scheme tags matching none of them are kept.
    own_glob = _resolve_own_tag_glob(workspace_root, project, projects)
    releasables_src = []
    if is_explicit_mode(workspace_root):
        releasables_src = load_releasables(workspace_root, projects)
    rel_of_extracted = resolve_releasable_for_project(project, releasables_src)
    foreign_globs = _other_member_globs(
        workspace_root, projects, releasables_src,
        exclude_project_names={package_name},
        exclude_releasable_names=(
            {rel_of_extracted.name} if rel_of_extracted is not None else frozenset()
        ),
    )

    # Clone the monorepo to target path
    _run_git(workspace_root, "clone", "--no-local", ".", target_repo_path)
    _ensure_git_identity(target_repo_path, workspace_root)

    # Keep only the package's directory, then hoist it to the repo root. Both
    # runs update ONE cumulative .git/filter-repo/commit-map.
    _run_filter_repo(target_repo_path, "--path", package_path, "--force")
    _run_filter_repo(
        target_repo_path, "--path-rename", f"{package_path}/:", "--force"
    )

    commit_map_path = os.path.join(
        target_repo_path, ".git", "filter-repo", "commit-map"
    )
    sha_map, _pruned = load_filter_repo_commit_map(commit_map_path)

    # Migrate changelog (writes verbatim old monorepo SHAs), then remap those
    # to the rewritten SHAs and drop entries whose commits were pruned.
    source_changes_dir = get_changes_dir(
        os.path.join(workspace_root, package_path)
    )
    target_changes_dir = os.path.join(target_repo_path, ".rlsbl", "changes")

    files_written = 0
    entries_migrated = 0
    if os.path.isdir(source_changes_dir):
        files_written, entries_migrated = _migrate_changelog_to_new_repo(
            source_changes_dir, target_changes_dir, package_path, workspace_root
        )
        _remap_and_prune(target_changes_dir, sha_map, target_repo_path)

    # Create .rlsbl/ config
    source_config = os.path.join(
        workspace_root, package_path, ".rlsbl", "config.json"
    )
    _create_rlsbl_config(target_repo_path, source_config)

    # Translate the package's monorepo-scheme tags to standalone v{version},
    # and prune foreign packages' tags.
    tags_translated, tags_deleted, _tags_left = _translate_extract_tags(
        target_repo_path, own_glob, foreign_globs, keep_own=False
    )

    # Commit the migrated + remapped + retagged state in the extracted repo.
    _commit_extracted_state(target_repo_path)

    # Update source monorepo: remove project from workspace.toml
    _remove_project_from_workspace(workspace_root, package_name, projects)

    return {
        "package_name": package_name,
        "target_path": target_repo_path,
        "package_path": package_path,
        "entries_migrated": entries_migrated,
        "files_written": files_written,
        "tags_translated": tags_translated,
        "tags_deleted": tags_deleted,
    }


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

    status = _run_git(source_repo_path, "status", "--porcelain")
    if status:
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
    tmp_root = tempfile.mkdtemp(prefix="rlsbl-absorb-")
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
        shutil.rmtree(tmp_root, ignore_errors=True)

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
    os.makedirs(rel_changes_dir, exist_ok=True)
    moved = 0

    pkg_unreleased = os.path.join(pkg_changes_dir, "unreleased.jsonl")
    if os.path.isfile(pkg_unreleased):
        entries = parse_jsonl(pkg_unreleased)
        if entries:
            rel_unreleased = os.path.join(rel_changes_dir, "unreleased.jsonl")
            with open(rel_unreleased, "a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(serialize_entry(entry) + "\n")
            moved += len(entries)

    for version, path in list_versioned_files(pkg_changes_dir):
        target = os.path.join(rel_changes_dir, f"{version}.jsonl")
        if os.path.isfile(target):
            continue
        entries = parse_jsonl(path)
        with open(target, "w", encoding="utf-8") as f:
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
    # Parse porcelain without stripping: each line is "XY <path>" and the
    # leading status columns must be preserved (a global strip would eat the
    # first line's leading space and corrupt its path).
    result = effects.run(
        ["git", "status", "--porcelain"],
        cwd=str(workspace_root), capture_output=True, text=True, check=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip().strip('"')
        if rest:
            paths.append(rest)
    if not paths:
        return
    commit_files(
        f"monorepo: absorb {name}",
        paths,
        cwd=str(workspace_root),
    )


def cmd_extract_releasable(
    workspace_root, releasable_name, target_repo_path, *, dry_run=False
):
    """Extract all member packages of a releasable into a new repository.

    If the releasable has one member, the result is a single-project repo.
    If it has multiple members, the result is a monorepo with workspace.toml.

    Steps:
    1. Resolve releasable members from workspace.toml
    2. Clone the monorepo
    3. Run ``git filter-repo --path <dir1> --path <dir2> ...`` for all
       member paths
    4. Migrate changelog for each member
    5. Create appropriate config (single-project or monorepo)
    6. Update source monorepo: remove all member projects from workspace.toml

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable to extract.
        target_repo_path: path where the new repo will be created.
        dry_run: if True, validate but do not perform the extraction.

    Returns:
        A dict with extraction details.
    """
    require_filter_repo()

    projects = load_workspace(workspace_root)
    releasables = load_releasables(workspace_root, projects)

    # Find the releasable
    target_releasable = None
    for rel in releasables:
        if rel.name == releasable_name:
            target_releasable = rel
            break
    if target_releasable is None:
        available = ", ".join(r.name for r in releasables)
        raise ExtractError(
            f"releasable '{releasable_name}' not found. Available: {available}"
        )

    member_projects = members_of(releasable_name, projects)
    if not member_projects:
        raise ExtractError(
            f"releasable '{releasable_name}' has no member packages"
        )

    if os.path.exists(target_repo_path):
        raise ExtractError(
            f"target path already exists: {target_repo_path}"
        )

    member_paths = [p.path for p in member_projects]
    member_names = [p.name for p in member_projects]
    is_multi = len(member_projects) > 1

    if dry_run:
        return {
            "releasable_name": releasable_name,
            "target_path": target_repo_path,
            "member_packages": member_names,
            "is_monorepo": is_multi,
            "dry_run": True,
        }

    # The releasable's tag glob (e.g. "core@v*") is the same for every member.
    own_glob = releasable_tag_glob(target_releasable.tag_format, releasable_name)

    # Resolve the other live members'/releasables' globs from the SOURCE
    # workspace (before it is mutated). These decide which scheme tags are
    # foreign (safe to prune); scheme tags matching none are kept.
    foreign_globs = _other_member_globs(
        workspace_root, projects, releasables,
        exclude_project_names=set(member_names),
        exclude_releasable_names={releasable_name},
    )

    # Clone the monorepo to target path
    _run_git(workspace_root, "clone", "--no-local", ".", target_repo_path)
    _ensure_git_identity(target_repo_path, workspace_root)

    # Keep only the member paths (one multi-path run). For a single-member
    # releasable, hoist the lone package to the repo root. Both runs update the
    # ONE cumulative .git/filter-repo/commit-map.
    filter_args = []
    for path in member_paths:
        filter_args.extend(["--path", path])
    filter_args.append("--force")
    _run_filter_repo(target_repo_path, *filter_args)

    if not is_multi:
        _run_filter_repo(
            target_repo_path, "--path-rename", f"{member_paths[0]}/:", "--force"
        )

    commit_map_path = os.path.join(
        target_repo_path, ".git", "filter-repo", "commit-map"
    )
    sha_map, _pruned = load_filter_repo_commit_map(commit_map_path)

    # Migrate changelogs, then remap + prune each member's changes dir.
    total_entries = 0
    total_files = 0
    for proj in member_projects:
        source_changes_dir = get_changes_dir(
            os.path.join(workspace_root, proj.path)
        )
        if is_multi:
            target_changes_dir = os.path.join(
                target_repo_path, proj.path, ".rlsbl", "changes"
            )
        else:
            target_changes_dir = os.path.join(
                target_repo_path, ".rlsbl", "changes"
            )

        if os.path.isdir(source_changes_dir):
            fw, em = _migrate_changelog_to_new_repo(
                source_changes_dir, target_changes_dir, proj.path, workspace_root
            )
            total_files += fw
            total_entries += em
            _remap_and_prune(target_changes_dir, sha_map, target_repo_path)

    # Create config + translate/keep tags.
    if is_multi:
        # Recreate the monorepo workspace.toml in the new repo, preserving the
        # [[releasables]] grouping and each member's releasable assignment so
        # the releasable-scheme tags stay valid -- so those tags are KEPT
        # (translate nothing); only foreign tags are pruned.
        new_projects = []
        from ...workspace import WorkspaceProject as WP
        for proj in member_projects:
            new_proj = {"path": proj.path, "name": proj.name, "releasable": releasable_name}
            new_projects.append(WP(new_proj))
        save_workspace(
            target_repo_path, new_projects, releasables=[target_releasable]
        )
        tags_translated, tags_deleted, _left = _translate_extract_tags(
            target_repo_path, own_glob, foreign_globs, keep_own=True
        )
    else:
        # Single-project repo: create .rlsbl/ config and translate the
        # releasable-scheme tags to standalone v{version}.
        source_config = os.path.join(
            workspace_root, member_paths[0], ".rlsbl", "config.json"
        )
        _create_rlsbl_config(target_repo_path, source_config)
        tags_translated, tags_deleted, _left = _translate_extract_tags(
            target_repo_path, own_glob, foreign_globs, keep_own=False
        )

    # Commit the migrated + remapped + retagged state in the extracted repo.
    _commit_extracted_state(target_repo_path)

    # Update source monorepo: remove all member projects
    remaining = [p for p in projects if p.name not in member_names]
    # Also remove the releasable definition if in explicit mode
    remaining_releasables = [r for r in releasables if r.name != releasable_name]
    save_workspace(workspace_root, remaining, releasables=remaining_releasables)

    return {
        "releasable_name": releasable_name,
        "target_path": target_repo_path,
        "member_packages": member_names,
        "is_monorepo": is_multi,
        "entries_migrated": total_entries,
        "files_written": total_files,
        "tags_translated": tags_translated,
        "tags_deleted": tags_deleted,
    }
