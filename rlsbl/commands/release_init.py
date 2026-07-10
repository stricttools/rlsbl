"""release init command: scaffold a release file by auto-detecting project targets."""

import os
import sys

from ..utils import commit_files


def run_cmd(project_root):
    """Create .rlsbl/releases/unreleased.toml with auto-detected targets.

    Args:
        project_root: Path to the project root directory.
    """
    import tomlkit

    from ..errors import ReleaseFileError
    from ..release_file import (
        check_legacy_release_file,
        get_release_file_path,
        is_pristine_release_file,
    )
    from ..targets import detect_targets
    from ..workspace import find_workspace_root, resolve_project

    # In monorepo mode, create the release file in the package's directory.
    # For releasable members (explicit mode), the file is scaffolded into
    # the releasable's own releases dir
    # (.rlsbl-monorepo/releasables/<name>/releases/), never under the
    # member's .rlsbl/ — same home as in-progress.json.
    start_path = str(project_root)
    project_dir = start_path
    releasable_dir = None
    monorepo_root = find_workspace_root(start_path)
    if monorepo_root:
        from ..workspace import is_explicit_mode
        if is_explicit_mode(monorepo_root):
            print(
                "Warning: this project belongs to a monorepo workspace that uses "
                "[[releasables]] (explicit mode). Batch releases should use "
                "'rlsbl monorepo release init' instead of 'rlsbl release init'.",
                file=sys.stderr,
            )
        project = resolve_project(monorepo_root, start_path)
        if project is not None:
            project_dir = os.path.join(monorepo_root, project["path"])
        from .release.release_state import resolve_releasable_dir
        releasable_dir = resolve_releasable_dir(project_dir, monorepo_root)

    try:
        check_legacy_release_file(project_dir, releasable_dir)
    except ReleaseFileError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    release_path = get_release_file_path(project_dir, releasable_dir=releasable_dir)

    def handle_existing() -> None:
        """Refuse-unless-pristine for an existing release file.

        Returns normally (caller should stop) if the file is a still-pristine
        scaffold (idempotent no-op). Calls sys.exit(1) if the file has been
        filled in by an operator -- never overwriting operator data.
        """
        with open(release_path, "r", encoding="utf-8") as f:
            existing = f.read()
        if is_pristine_release_file(existing):
            print(
                f"{release_path} already exists and is pristine "
                f"(no bump/description filled in); nothing to do."
            )
            return
        print(
            f"Error: {release_path} already exists and has been filled in. "
            f"Refusing to overwrite it. Edit it directly, or delete it to "
            f"re-scaffold.",
            file=sys.stderr,
        )
        sys.exit(1)

    if os.path.exists(release_path):
        handle_existing()
        return

    entries = detect_targets(project_dir)
    if not entries:
        print("Error: no targets detected in the current directory.", file=sys.stderr)
        sys.exit(1)

    target_names = [e.name for e in entries]

    doc = tomlkit.document()
    doc.add(tomlkit.comment("Version bump type: patch, minor, major, hotfix, or prerelease"))
    doc.add("bump", "")
    doc.add(tomlkit.comment("Short description of this release (required)"))
    doc.add("description", "")
    doc.add(tomlkit.comment("Optional context explaining why these changes were made"))
    doc.add("context", "")
    doc.add(tomlkit.comment("Pre-release identifier: alpha, beta, rc, or stable"))
    doc.add(tomlkit.comment('preid = ""'))
    doc.add(tomlkit.comment("Set to true to generate a blog post for this release"))
    doc.add(tomlkit.comment("blog = false"))
    doc.add("include", target_names)
    doc.add("exclude", [])

    # Add per-target config sections for Flutter target
    flutter_targets = [n for n in target_names if n == "flutter"]
    if flutter_targets:
        targets_table = tomlkit.table(is_super_table=True)
        for ft in flutter_targets:
            t = tomlkit.table()
            t.add("mode", "build")
            targets_table.add(ft, t)
        doc.add("targets", targets_table)

    releases_dir = os.path.dirname(release_path)
    os.makedirs(releases_dir, exist_ok=True)

    # Atomic exclusive-create closes the TOCTOU: the earlier exists() check and
    # this write are far apart (detect_targets/doc-build run in between), so a
    # racing init could create and fill the file. O_EXCL guarantees we only
    # write when the file is truly absent; on collision we re-run the
    # refuse-unless-pristine check against whatever the racer wrote.
    try:
        fd = os.open(release_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        handle_existing()
        return
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        tomlkit.dump(doc, f)

    commit_files("release: scaffold unreleased.toml", [release_path], allow_failure=True)

    print(release_path)
