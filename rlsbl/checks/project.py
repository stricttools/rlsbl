"""Project checks (tag: project) validating version, name, license, and description consistency, config schema, and scaffold conflicts.

Checks: lock, version-consistency, name-consistency, license-consistency,
description-consistency, private-hook-stale, config-schema, license-file,
publish-mode-workflow, npm-private-mismatch, target-version-readable,
dunder-version-missing, selfdoc-version-drift, scaffold-conflicts,
cross-repo-path-sources.
"""

import json
import os
import tomllib

from ..errors import ConfigError


def find_conflicted_scaffold_files(project_root):
    """Return ``(relpath, first_conflict_line)`` tuples for scaffold files
    with unresolved git merge conflict markers, sorted by path.

    Scans three file sets (skipping files that don't exist):
    - every file listed in ``.rlsbl/managed-files.json`` (the
      scaffold-managed registry; missing or malformed registry is skipped)
    - all files under ``.github/workflows/``
    - all files under ``.rlsbl/`` (recursively)

    A file is conflicted when it contains a line starting with
    ``'<<<<<<< '`` AND a line starting with ``'>>>>>>> '``. Requiring
    both avoids false positives on bare ``'======='`` lines (e.g.
    setext heading underlines). ``first_conflict_line`` is the 1-based
    line number of the first ``'<<<<<<< '`` marker in the file.

    Shared by the ``scaffold-conflicts`` check and the pre-mutation
    guard in ``rlsbl release run``.
    """
    root_str = str(project_root)
    candidates = set()

    registry_path = os.path.join(root_str, ".rlsbl", "managed-files.json")
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for rel in data.get("files", {}):
                candidates.add(os.path.join(root_str, rel))
        except (OSError, json.JSONDecodeError):
            pass  # malformed registry -- skip, don't crash the check

    workflows_dir = os.path.join(root_str, ".github", "workflows")
    if os.path.isdir(workflows_dir):
        for entry in os.listdir(workflows_dir):
            candidates.add(os.path.join(workflows_dir, entry))

    rlsbl_dir = os.path.join(root_str, ".rlsbl")
    for dirpath, _dirnames, filenames in os.walk(rlsbl_dir):
        for filename in filenames:
            candidates.add(os.path.join(dirpath, filename))

    conflicted = []
    for filepath in candidates:
        if not os.path.isfile(filepath):
            continue
        first_start_line = None
        has_end = False
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if first_start_line is None and line.startswith("<<<<<<< "):
                        first_start_line = lineno
                    elif line.startswith(">>>>>>> "):
                        has_end = True
                    if first_start_line is not None and has_end:
                        break
        except (OSError, UnicodeDecodeError):
            continue
        if first_start_line is not None and has_end:
            conflicted.append(
                (os.path.relpath(filepath, root_str), first_start_line)
            )

    return sorted(conflicted)


def find_cross_repo_path_sources(project_root, boundary_root=None):
    """Return ``(package, declared_path, resolved_path)`` tuples for
    ``[tool.uv.sources]`` entries in ``project_root/pyproject.toml`` whose
    ``path`` resolves outside *boundary_root* (defaults to *project_root*).

    Relative paths are resolved against the pyproject.toml's directory,
    matching uv's resolution rule. ``workspace = true`` sources and in-repo
    path sources are legal and never reported. Returns an empty list when
    pyproject.toml is missing, unparseable, or has no sources table.

    A non-string ``path`` value (malformed TOML) is reported as an offender
    whose third element is an "invalid path value" message naming the source
    and file, so consumers fail the check instead of crashing.

    Shared by the ``cross-repo-path-sources`` check and the pre-mutation
    guard in ``rlsbl release run``.
    """
    root_str = str(project_root)
    pyproject_path = os.path.join(root_str, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        return []
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []

    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    if not isinstance(sources, dict):
        return []

    boundary = os.path.realpath(str(boundary_root) if boundary_root else root_str)
    offenders = []
    for package, spec in sources.items():
        # A source is a single table or a list of marker-gated tables.
        entries = spec if isinstance(spec, list) else [spec]
        for entry in entries:
            if not isinstance(entry, dict) or "path" not in entry:
                continue
            declared = entry["path"]
            if not isinstance(declared, str):
                # Malformed TOML (e.g. path = 123 or a table) must surface
                # as a check failure naming the entry, never a TypeError.
                offenders.append((
                    package,
                    repr(declared),
                    f"invalid path value for source '{package}' in "
                    f"{pyproject_path} (expected string)",
                ))
                continue
            if os.path.isabs(declared):
                resolved = os.path.realpath(declared)
            else:
                resolved = os.path.realpath(os.path.join(root_str, declared))
            if resolved != boundary and not resolved.startswith(boundary + os.sep):
                offenders.append((package, declared, resolved))
    return offenders


def _get_releasable_version_for_project(ctx):
    """Return the releasable version for the project in ctx, or None.

    Returns a version string when ALL of the following hold:
    - The project is inside a monorepo (ctx.workspace_root is not None)
    - The workspace uses explicit releasable mode ([[releasables]] present)
    - The current project has ``releasable = "<name>"`` (belongs to a releasable)
    - The releasable's version file exists and is non-empty

    Returns None otherwise (implicit mode, non-releasable project, missing
    version file, or not in a monorepo). Callers should fall through to the
    standard per-target consistency check when None is returned.
    """
    workspace_root = getattr(ctx, "workspace_root", None)
    if workspace_root is None:
        return None

    from ..workspace import is_explicit_mode, read_releasable_version, resolve_project
    from ..errors import WorkspaceError

    ws_root = str(workspace_root)
    if not is_explicit_mode(ws_root):
        return None

    project = resolve_project(ws_root, str(ctx.project_root))
    if project is None:
        return None

    rel_val = project.releasable
    if not isinstance(rel_val, str):
        return None

    try:
        return read_releasable_version(ws_root, rel_val)
    except WorkspaceError:
        return None


def _virtual_root_skip_reason(ctx):
    """Return a skip reason string when ctx.project_root is a virtual uv
    workspace root (has [tool.uv.workspace] but no [project] table), else None.
    """
    from ..utils import is_virtual_uv_root

    if is_virtual_uv_root(str(ctx.project_root)):
        return "virtual uv workspace root (no [project] table -- not a release target)"
    return None


def register_project_checks(app):
    """Register project-tag checks on *app*."""

    @app.warn_check("lock")
    def check_lock(ctx, reporter):
        """Detect stale lock files."""
        from ..lock import is_stale

        root_str = str(ctx.project_root)
        stale_paths = []
        if is_stale(lock_path=os.path.join(root_str, ".rlsbl", "lock"), project_root=ctx.project_root):
            stale_paths.append(".rlsbl/lock")
        if is_stale(lock_path=os.path.join(root_str, ".rlsbl-monorepo", "lock"), project_root=ctx.project_root):
            stale_paths.append(".rlsbl-monorepo/lock")

        if stale_paths:
            reporter.warn(f"stale lock file exists at {', '.join(stale_paths)}")
            return reporter.found(f"stale lock file exists at {', '.join(stale_paths)}")
        return reporter.passed("no lock file")

    @app.error_check("version-consistency")
    def check_version_consistency(ctx, reporter):
        """All detected targets must report the same version.

        In explicit releasable mode (when the project belongs to a named
        releasable with a version file), the releasable version is the
        source of truth. Published (publish_mode != "none") member manifests are
        checked against the releasable version -- a mismatch is an error.

        In implicit mode (no [[releasables]]), all targets within the
        project must agree on the same version.
        """
        skip_reason = _virtual_root_skip_reason(ctx)
        if skip_reason is not None:
            return reporter.skipped(skip_reason)

        # In explicit mode, the releasable version file is authoritative.
        # Published members' manifests must match it.
        releasable_version = _get_releasable_version_for_project(ctx)
        if releasable_version is not None:
            from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir_for_ctx
            from ..member_context import resolve_member_context

            rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
            member = resolve_member_context(
                str(ctx.project_root), releasable_config_dir=rel_dir,
            )
            if member.publish_mode == "none":
                return reporter.passed(
                    f"{releasable_version} (publish-suppressed member, version file only)"
                )

            # Check manifest versions of published member
            target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
            mismatches = []
            for name, path in target_entries:
                target = TARGETS.get(name)
                if target is None:
                    continue
                try:
                    manifest_version = target.read_version(path)
                except Exception:
                    continue
                if manifest_version is not None and manifest_version != releasable_version:
                    # Tolerate PEP 440 normalization for pypi targets
                    if name == "pypi":
                        from ..targets.pypi import PypiTarget
                        if PypiTarget.format_version(None, releasable_version) == manifest_version:
                            continue
                    mismatches.append(
                        f"{name}={manifest_version} (expected {releasable_version})"
                    )

            if mismatches:
                reporter.error(
                    f"published member manifest version mismatch vs releasable "
                    f"version {releasable_version}: {', '.join(mismatches)}"
                )
                return reporter.found(
                    f"published member manifest version mismatch vs releasable "
                    f"version {releasable_version}: {', '.join(mismatches)}"
                )
            return reporter.passed(
                f"{releasable_version} (releasable version, manifests match)"
            )

        from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir_for_ctx

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
        if not target_entries:
            reporter.warn("no targets detected")
            return reporter.found("no targets detected")

        versions = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                v = target.read_version(path)
                versions[name] = v
            except Exception as e:
                import sys
                print(f"Warning: could not read version from {name}: {e}", file=sys.stderr)
                versions[name] = None

        # Include selfdoc.json in version consistency checks. selfdoc.json
        # is not a release target, but its version must stay in sync.
        detected_names = {name for name, _path in target_entries}
        if "selfdoc" not in detected_names:
            selfdoc_path = os.path.join(str(ctx.project_root), "selfdoc.json")
            if os.path.exists(selfdoc_path):
                try:
                    with open(selfdoc_path, "r", encoding="utf-8") as f:
                        selfdoc_data = json.load(f)
                    versions["selfdoc"] = selfdoc_data.get("version", "0.0.0")
                except (OSError, json.JSONDecodeError) as e:
                    import sys
                    print(f"Warning: could not read selfdoc.json: {e}", file=sys.stderr)
                    versions["selfdoc"] = None

        unique = set(v for v in versions.values() if v is not None)
        if len(unique) == 0:
            reporter.warn("no targets reported a version")
            return reporter.found("no targets reported a version")
        if len(unique) > 1:
            detail = ", ".join(f"{n}={v}" for n, v in versions.items() if v is not None)
            reporter.error(f"version mismatch: {detail}")
            return reporter.found(f"version mismatch: {detail}")

        version = unique.pop()
        return reporter.passed(f"{version} across {len(versions)} target(s)")

    @app.warn_check("name-consistency")
    def check_name_consistency(ctx, reporter):
        """All detected targets must report the same package name."""
        skip_reason = _virtual_root_skip_reason(ctx)
        if skip_reason is not None:
            return reporter.skipped(skip_reason)

        from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir_for_ctx
        from ..targets.utils import normalize_go, normalize_npm, normalize_pypi

        def _normalize_name(target_name, raw_name):
            normalizers = {
                "npm": normalize_npm,
                "pypi": normalize_pypi,
                "go": normalize_go,
            }
            normalizer = normalizers.get(target_name, str.lower)
            return normalizer(raw_name)

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
        if not target_entries:
            reporter.warn("no targets detected")
            return reporter.found("no targets detected")

        names = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                n = target.read_name(path, ctx=ctx)
                names[name] = n
            except Exception as e:
                import sys
                print(f"Warning: could not read name from {name}: {e}", file=sys.stderr)
                names[name] = None

        have_name = {k: v for k, v in names.items() if v is not None}
        if not have_name:
            reporter.warn("no targets reported a name")
            return reporter.found("no targets reported a name")

        missing = [k for k, v in names.items() if v is None]
        normalized = {k: _normalize_name(k, v) for k, v in have_name.items()}
        unique = set(normalized.values())

        if len(unique) == 1:
            raw_name = next(iter(have_name.values()))
            msg = f"{raw_name} across {len(target_entries)} target(s)"
            if missing:
                msg += f" (no name from: {', '.join(missing)})"
            return reporter.passed(msg)

        detail = ", ".join(f"{k}={v}" for k, v in have_name.items())
        reporter.warn(f"name mismatch: {detail}")
        return reporter.found(f"name mismatch: {detail}")

    @app.warn_check("license-consistency")
    def check_license_consistency(ctx, reporter):
        """All detected targets must report the same license."""
        from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir_for_ctx

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
        if not target_entries:
            return reporter.passed("no targets declare a license")

        licenses = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                meta = target.read_metadata(path)
                if "license" in meta:
                    licenses[name] = meta["license"]
            except Exception as e:
                import sys
                print(f"Warning: could not read metadata from {name}: {e}", file=sys.stderr)

        if len(licenses) == 0:
            return reporter.passed("no targets declare a license")
        if len(licenses) < 2:
            return reporter.passed(f"only {len(licenses)} target(s) declare a license")

        unique = set(v.lower() for v in licenses.values())
        if len(unique) == 1:
            license_val = next(iter(licenses.values()))
            return reporter.passed(f"{license_val} across {len(licenses)} target(s)")

        detail = ", ".join(f"{k}={v}" for k, v in licenses.items())
        reporter.warn(f"license mismatch: {detail}")
        return reporter.found(f"license mismatch: {detail}")

    @app.warn_check("description-consistency")
    def check_description_consistency(ctx, reporter):
        """All detected targets must report the same description."""
        from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir_for_ctx

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
        if not target_entries:
            return reporter.passed("no targets declare a description")

        descriptions = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                meta = target.read_metadata(path)
                if "description" in meta:
                    descriptions[name] = meta["description"]
            except Exception as e:
                import sys
                print(f"Warning: could not read metadata from {name}: {e}", file=sys.stderr)

        if len(descriptions) == 0:
            return reporter.passed("no targets declare a description")
        if len(descriptions) < 2:
            return reporter.passed(f"only {len(descriptions)} target(s) declare a description")

        unique = set(descriptions.values())
        if len(unique) == 1:
            desc_val = next(iter(descriptions.values()))
            return reporter.passed(f"{desc_val} across {len(descriptions)} target(s)")

        detail = ", ".join(f"{k}={v}" for k, v in descriptions.items())
        reporter.warn(f"description mismatch: {detail}")
        return reporter.found(f"description mismatch: {detail}")

    @app.error_check("private-hook-stale")
    def check_private_hook_stale(ctx, reporter):
        """Detect legacy private asset upload code in post-release hook.

        Checks the per-package hook at ``.rlsbl/hooks/post-release.sh``.
        In explicit releasable mode, also checks the releasable-level hook
        at ``.rlsbl-monorepo/releasables/{name}/hooks/post-release.sh``.
        """
        legacy_marker = "Post-release hook for private repositories"
        stale_paths = []
        hooks_found = False

        # Per-package hook
        hook_path = os.path.join(str(ctx.project_root), ".rlsbl", "hooks", "post-release.sh")
        if os.path.exists(hook_path):
            hooks_found = True
            with open(hook_path, "r", encoding="utf-8") as f:
                content = f.read()
            if legacy_marker in content:
                stale_paths.append(os.path.relpath(hook_path, str(ctx.project_root)))

        # Releasable-level hook (explicit mode only)
        workspace_root = getattr(ctx, "workspace_root", None)
        if workspace_root is not None:
            from ..workspace import is_explicit_mode, resolve_project
            ws_root = str(workspace_root)
            if is_explicit_mode(ws_root):
                project = resolve_project(ws_root, str(ctx.project_root))
                if project is not None:
                    rel_val = project.releasable
                    if isinstance(rel_val, str):
                        from ..workspace_types import get_releasable_hook_path
                        rel_hook = get_releasable_hook_path(ws_root, rel_val, "post-release.sh")
                        if os.path.exists(rel_hook):
                            hooks_found = True
                            with open(rel_hook, "r", encoding="utf-8") as f:
                                rel_content = f.read()
                            if legacy_marker in rel_content:
                                stale_paths.append(os.path.relpath(rel_hook, str(ctx.project_root)))

        if stale_paths:
            msg = (
                f"Post-release hook(s) contain legacy private asset upload code: "
                f"{', '.join(stale_paths)}. "
                "Asset upload is now a built-in release step. "
                "Run `rlsbl scaffold` to get the standard hook template."
            )
            reporter.error(msg)
            return reporter.found(msg)
        if not hooks_found:
            return reporter.passed("no post-release hook")
        return reporter.passed("no legacy private hook code")

    @app.error_check("config-schema")
    def check_config_schema(ctx, reporter):
        """Validate config schema: publish_mode, banned keys, and pipelines."""
        skip_reason = _virtual_root_skip_reason(ctx)
        if skip_reason is not None:
            return reporter.skipped(skip_reason)

        config = ctx.config
        errors = []

        # Validate banned keys and structural invariants
        from ..config import validate_config_schema as _validate_config_schema
        try:
            _validate_config_schema(config, project_dir=str(ctx.project_root))
        except ConfigError as e:
            errors.append(str(e))

        # Validate pipelines config if present
        from ..config import validate_pipelines_config
        try:
            validate_pipelines_config(config)
        except ConfigError as e:
            errors.append(str(e))

        # Validate pipeline target links (separate-but-linked config shape)
        from ..config import validate_pipeline_target_links
        try:
            validate_pipeline_target_links(config)
        except ConfigError as e:
            errors.append(str(e))

        # Validate the optional per-target test config block if present
        from ..config import validate_test_config
        try:
            validate_test_config(config)
        except ConfigError as e:
            errors.append(str(e))

        if errors:
            for err in errors:
                reporter.error(err)
            return reporter.found(f"{len(errors)} config error(s)")
        return reporter.passed("config schema valid")

    @app.error_check("license-file")
    def check_license_file(ctx, reporter):
        """LICENSE file must exist, be non-empty, and have no template variables."""
        import re as _re

        license_path = os.path.join(str(ctx.project_root), "LICENSE")
        if not os.path.exists(license_path):
            reporter.error("LICENSE file not found in project root")
            return reporter.found("LICENSE file not found in project root")

        try:
            size = os.path.getsize(license_path)
        except OSError:
            reporter.error("cannot read LICENSE file")
            return reporter.found("cannot read LICENSE file")

        if size == 0:
            reporter.error("LICENSE file is empty")
            return reporter.found("LICENSE file is empty")

        with open(license_path, "r", encoding="utf-8") as f:
            content = f.read()

        template_vars = _re.findall(r"\{\{\w+(?:\.\w+)*\}\}", content)
        if template_vars:
            msg = f"LICENSE contains unreplaced template variable(s): {', '.join(template_vars)}"
            reporter.error(msg)
            return reporter.found(msg)

        return reporter.passed("LICENSE file valid")

    @app.error_check("publish-mode-workflow")
    def check_publish_mode_workflow(ctx, reporter):
        """publish_mode "none" repos must not have publish workflows."""
        skip_reason = _virtual_root_skip_reason(ctx)
        if skip_reason is not None:
            return reporter.skipped(skip_reason)

        from ..config import get_publish_mode
        try:
            mode = get_publish_mode(ctx.config)
        except ConfigError as e:
            reporter.error(str(e))
            return reporter.found(str(e))
        if mode != "none":
            return reporter.passed('publish_mode is not "none"')

        import glob

        root_str = str(ctx.project_root)
        wf_dir = os.path.join(root_str, ".github", "workflows")
        if not os.path.isdir(wf_dir):
            return reporter.passed("no .github/workflows/ directory")

        publish_files = []
        for filepath in glob.glob(os.path.join(wf_dir, "*.yml")):
            basename = os.path.basename(filepath)
            if "publish" in basename.lower():
                publish_files.append(basename)
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                if "release:" in content and "published" in content:
                    publish_files.append(basename)
            except (OSError, UnicodeDecodeError):
                continue

        if publish_files:
            msg = f'publish_mode "none" repo has publish workflow(s): {", ".join(sorted(publish_files))}'
            reporter.error(msg)
            return reporter.found(msg)
        return reporter.passed('no publish workflows in publish_mode "none" repo')

    @app.error_check("npm-private-mismatch")
    def check_npm_private_mismatch(ctx, reporter):
        """package.json private:true must not contradict publish_mode "ci"."""
        root_str = str(ctx.project_root)
        pkg_path = os.path.join(root_str, "package.json")
        if not os.path.exists(pkg_path):
            return reporter.skipped("no package.json")

        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return reporter.skipped("cannot read package.json")

        npm_private = pkg.get("private", False)
        from ..config import get_publish_mode
        try:
            mode = get_publish_mode(ctx.config)
        except ConfigError as e:
            reporter.error(str(e))
            return reporter.found(str(e))

        if npm_private is True and mode != "none":
            msg = (
                'package.json has "private": true but .rlsbl/config.json has '
                f'"publish_mode": "{mode}" (publishing enabled)'
            )
            reporter.error(msg)
            return reporter.found(msg)
        return reporter.passed("npm private flag consistent with publish_mode")

    @app.error_check("target-version-readable")
    def check_target_version_readable(ctx, reporter):
        """Every detected target must be able to read its version without error."""
        skip_reason = _virtual_root_skip_reason(ctx)
        if skip_reason is not None:
            return reporter.skipped(skip_reason)

        from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir_for_ctx

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
        if not target_entries:
            return reporter.skipped("no targets detected")

        errors = []
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                target.read_version(path)
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        if errors:
            for err in errors:
                reporter.error(err)
            return reporter.found(f"{len(errors)} target(s) cannot read version")
        return reporter.passed(f"all {len(target_entries)} target(s) version readable")

    @app.error_check("dunder-version-missing")
    def check_dunder_version_missing(ctx, reporter):
        """PyPI targets with a version constant must use __version__."""
        import ast
        import re as _re

        from ..targets import detect_targets, resolve_releasable_config_dir_for_ctx
        from ..targets.utils import detect_python_package_root
        from ..targets.pypi import has_any_dunder_version
        from ..errors import VersionError

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)

        # Step 3: skip if no pypi target
        if not any(name == "pypi" for name, _path in target_entries):
            return reporter.skipped("no pypi target")

        # Step 4: detect package root
        root_str = str(ctx.project_root)
        try:
            pkg_root = detect_python_package_root(root_str)
        except VersionError as e:
            reporter.error(str(e))
            return reporter.found(str(e))

        if pkg_root is None:
            return reporter.passed("cannot determine package root")

        # Step 5: check __init__.py existence
        init_path = os.path.join(root_str, pkg_root, "__init__.py")
        if not os.path.exists(init_path):
            return reporter.passed("no __init__.py (namespace package)")

        # Step 6: read file content
        with open(init_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Step 7: check for __version__
        if has_any_dunder_version(content):
            return reporter.passed("__version__ defined")

        # Step 8: scan AST for version-like constants without __version__
        rel_path = os.path.relpath(init_path, root_str)
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return reporter.passed("cannot parse __init__.py")

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and "version" in target.id.lower()
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                        and _re.search(r"\d+\.\d+", node.value.value)
                    ):
                        msg = (
                            f'{target.id} = "{node.value.value}" found in '
                            f"{rel_path} but no __version__; rename to __version__"
                        )
                        reporter.error(msg)
                        return reporter.found(msg)

        # Step 9: no version constant at all -- pure re-export module
        return reporter.passed("no version constant in __init__.py")

    @app.error_check("selfdoc-version-drift")
    def check_selfdoc_version_drift(ctx, reporter):
        """selfdoc.json version must match the primary target's version."""
        root_str = str(ctx.project_root)
        selfdoc_path = os.path.join(root_str, "selfdoc.json")
        if not os.path.exists(selfdoc_path):
            return reporter.skipped("no selfdoc.json")

        try:
            with open(selfdoc_path, "r", encoding="utf-8") as f:
                selfdoc_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return reporter.skipped("cannot read selfdoc.json")

        selfdoc_version = selfdoc_data.get("version")
        if selfdoc_version is None:
            return reporter.skipped("selfdoc.json has no version field")

        from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir_for_ctx

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(root_str, releasable_config_dir=rel_dir)
        if not target_entries:
            return reporter.skipped("no targets detected")

        first_name, first_path = target_entries[0]
        target = TARGETS[first_name]
        try:
            primary_version = target.read_version(first_path)
        except Exception:
            return reporter.skipped("cannot read primary target version")

        if primary_version is None:
            return reporter.skipped("primary target reports no version")

        if selfdoc_version != primary_version:
            msg = (
                f"selfdoc.json version ({selfdoc_version}) != "
                f"primary target {first_name} version ({primary_version})"
            )
            reporter.error(msg)
            return reporter.found(msg)
        return reporter.passed(f"selfdoc.json version matches ({selfdoc_version})")

    @app.error_check("scaffold-conflicts")
    def check_scaffold_conflicts(ctx, reporter):
        """Scaffold-managed files must not contain unresolved merge conflict markers."""
        conflicted = find_conflicted_scaffold_files(ctx.project_root)
        if conflicted:
            for path, line in conflicted:
                reporter.error(f"{path}:{line}")
            return reporter.found(
                f"{len(conflicted)} scaffold file(s) with unresolved "
                "merge conflict markers"
            )
        return reporter.passed("no unresolved merge conflict markers")

    @app.error_check("cross-repo-path-sources")
    def check_cross_repo_path_sources(ctx, reporter):
        """[tool.uv.sources] path entries must stay inside the repository."""
        root_str = str(ctx.project_root)
        if not os.path.isfile(os.path.join(root_str, "pyproject.toml")):
            return reporter.skipped("no pyproject.toml")

        workspace_root = getattr(ctx, "workspace_root", None)
        boundary = str(workspace_root) if workspace_root else root_str
        offenders = find_cross_repo_path_sources(root_str, boundary_root=boundary)
        if offenders:
            for pkg, declared, resolved in offenders:
                reporter.error(f'{pkg}: path = "{declared}" resolves to {resolved}')
            return reporter.found(
                f"{len(offenders)} [tool.uv.sources] path source(s) resolve "
                "outside the repository -- depend on the registry release and "
                "keep local overrides in dev-sources.toml.local-only"
            )
        return reporter.passed("no cross-repo path sources")

    @app.error_check("dev-overlay-drift")
    def check_dev_overlay_drift(ctx, reporter):
        """Declared dev-sync overlays must remain editable-installed.

        `rlsbl dev sync` overlays local editable checkouts of sibling projects
        onto the locked venv, then writes a sentinel
        (dev-overlays-state.toml.local-only) recording each overlaid package,
        its checkout path, and version. Any later bare `uv sync`/`uv run`
        silently reinstalls the locked registry wheel over an overlay, so the
        consuming project's tests then run against stale RELEASED dependency
        code with no error at all. This check reads the sentinel and inspects
        the venv's dist-info direct_url.json for each overlay: a package that
        is no longer an editable install of its declared path is a hard failure
        naming the package and the exact `rlsbl dev sync` remediation.
        """
        from ..overlay_state import (
            OVERLAY_HEALTHY,
            MalformedSentinelError,
            classify_overlay,
            inspect_installed,
            load_sentinel,
        )

        root = str(ctx.project_root)
        try:
            sentinel = load_sentinel(root)
        except MalformedSentinelError as e:
            reporter.error(str(e))
            return reporter.found(str(e))
        if sentinel is None:
            return reporter.skipped("no dev overlays declared (no sentinel)")
        if not sentinel:
            return reporter.skipped("sentinel declares no overlays")

        drifted = []
        for entry in sentinel:
            installed = inspect_installed(root, entry["package"])
            state, detail = classify_overlay(entry, installed)
            if state != OVERLAY_HEALTHY:
                drifted.append(detail)

        if drifted:
            for detail in drifted:
                reporter.error(detail)
            return reporter.found(
                f"{len(drifted)} of {len(sentinel)} dev overlay(s) wiped or "
                "missing -- a bare `uv sync`/`uv run` reinstalled registry "
                "wheels; run `rlsbl dev sync` to restore editable overlays"
            )
        return reporter.passed(f"all {len(sentinel)} dev overlay(s) editable-installed")
