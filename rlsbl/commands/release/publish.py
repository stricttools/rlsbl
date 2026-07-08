"""Publishing helpers: selfblog blog post generation, GitHub Release asset uploads, and stale dependency advisory for post-release notifications."""

import json
import os
import shutil
import subprocess
import sys
import tempfile

from .validate import HookError, ReleaseValidationError

# Names like run, require_tool, load_pipelines, warn_exception are imported
# from the parent package at call time (via `from . import X`) so that
# test patches on `rlsbl.commands.release.X` are picked up correctly.


def _run_selfblog_post_generate(flags, *, project_dir=None, release_config=None,
                                new_version=None, current_version=None,
                                bump_type=None, changelog_entry=None, tag=None,
                                releases_dir=None):
    """Generate a blog post via selfblog during release.

    Called when release_config.blog is True and selfdoc.json exists.
    Writes the changelog entry to a temp file and invokes
    ``selfblog post generate --from-release`` with all release metadata.

    The generated post file and updated manifest are picked up by the
    hook-generated-files mechanism (dirty snapshot diff) and included
    in the release commit.
    """
    from . import extract_github_repo_from_remote, require_tool, run

    check_dir = project_dir if project_dir else "."

    if not release_config or not release_config.blog:
        return True

    selfdoc_config = os.path.join(check_dir, "selfdoc.json")
    if not os.path.exists(selfdoc_config):
        return True

    if flags.get("dry-run"):
        print(f"Would run: selfblog post generate --from-release --version {new_version}")
        return True

    if not require_tool("selfblog", fatal=False):
        print(
            "Note: blog = true but selfblog is not installed. Skipping blog post generation."
        )
        return True

    print("Generating blog post via selfblog...")

    # Write changelog entry to a temp file
    tmp_changelog = None
    try:
        tmp_changelog = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="rlsbl-changelog-",
            delete=False, encoding="utf-8",
        )
        tmp_changelog.write(changelog_entry or "")
        tmp_changelog.close()

        # Assemble CLI args
        cmd = ["selfblog", "post", "generate", "--from-release"]
        cmd.extend(["--version", new_version or ""])
        if current_version:
            cmd.extend(["--prev-version", current_version])
        if bump_type:
            cmd.extend(["--bump-type", bump_type])
        if release_config.description:
            cmd.extend(["--description", release_config.description])
        if release_config.context:
            cmd.extend(["--context", release_config.context])
        cmd.extend(["--changelog-file", tmp_changelog.name])

        # Body file (optional). ``releases_dir`` is the resolved releases
        # dir (releasable-level in explicit releasable mode).
        _releases_dir = releases_dir or os.path.join(check_dir, ".rlsbl", "releases")
        blog_body_path = os.path.join(_releases_dir, "unreleased.md")
        if os.path.exists(blog_body_path):
            cmd.extend(["--body-file", blog_body_path])

        # Project name from selfdoc config or directory name
        try:
            with open(selfdoc_config, "r", encoding="utf-8") as f:
                sd_config = json.load(f)
            project_name = sd_config.get("project_name") or sd_config.get("name") or os.path.basename(os.path.abspath(check_dir))
        except Exception:
            project_name = os.path.basename(os.path.abspath(check_dir))
        cmd.extend(["--project-name", project_name])

        # Release URL (GitHub release URL pattern)
        try:
            remote = run("git", ["remote", "get-url", "origin"])
            repo_path = extract_github_repo_from_remote(remote)
            if repo_path:
                release_url = f"https://github.com/{repo_path}/releases/tag/{tag or ''}"
                cmd.extend(["--release-url", release_url])
        except Exception:
            pass  # Best-effort; release URL is optional

        subprocess.run(cmd, cwd=project_dir, check=True)
    except subprocess.CalledProcessError as e:
        raise HookError(
            f"selfblog post generate failed (exit code {e.returncode})."
        ) from e
    finally:
        if tmp_changelog and os.path.exists(tmp_changelog.name):
            os.unlink(tmp_changelog.name)

    return True


def _print_stale_dep_advisory(monorepo_name, new_version, monorepo_root=None):
    """Print advisory about downstream packages with stale constraints.

    After releasing a package, checks if any workspace package that depends
    on the just-released package has a constraint that no longer satisfies
    the new version. Prints to stderr as a non-blocking advisory.
    """
    from . import load_workspace

    try:
        from ..monorepo import _evaluate_constraint
        from ...workspace_graph import WorkspaceGraph

        ws_root = monorepo_root or "."
        projects = load_workspace(ws_root)
        graph = WorkspaceGraph(ws_root, projects)

        # Find direct dependents of the released package
        dependents = graph.dependents(monorepo_name)
        if not dependents:
            return

        stale_lines = []
        for dep_name in dependents:
            deps = graph.dependencies(dep_name)
            for dep in deps:
                if dep.name != monorepo_name:
                    continue
                if dep.dep_type != "versioned":
                    continue
                status = _evaluate_constraint(dep.constraint, new_version)
                if status == "outdated":
                    stale_lines.append(
                        f"  {dep_name} depends on {monorepo_name} "
                        f"{dep.constraint} but {monorepo_name} is now {new_version}\n"
                        f"    Suggested: update to >={new_version}"
                    )

        if stale_lines:
            print("! Stale dependency constraints:", file=sys.stderr)
            for line in stale_lines:
                print(line, file=sys.stderr)
    except Exception as e:
        from ...utils import warn_exception
        warn_exception("stale dependency advisory check failed", e)


def _prefix_artifact(artifact_path, member_name):
    """Prefix an artifact filename with the member name for deterministic naming.

    E.g., ``/dist/mylib-0.1.0.tar.gz`` -> ``/dist/core--mylib-0.1.0.tar.gz``
    where ``core`` is the member name.
    """
    dirname = os.path.dirname(artifact_path)
    basename = os.path.basename(artifact_path)
    prefixed = f"{member_name}--{basename}"
    new_path = os.path.join(dirname, prefixed)
    os.rename(artifact_path, new_path)
    return new_path


def _upload_assets_for_config(
    tag, new_version, log, flags, config, project_dir, ctx, *,
    member_name=None,
):
    """Build and upload assets for a single config (member or representative).

    When ``member_name`` is set, artifact filenames are prefixed with the
    member name to ensure deterministic, non-colliding names across members.
    """
    from . import load_pipelines, run_gh

    pipelines_cfg = config.get("pipelines", {})
    if not isinstance(pipelines_cfg, dict):
        return

    pipelines_with_assets = {}
    for name, entry in pipelines_cfg.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("assets") or entry.get("custom_assets"):
            pipelines_with_assets[name] = entry

    if not pipelines_with_assets:
        return

    dry_run = flags.get("dry-run", False)
    all_pipelines = load_pipelines(config)

    for name, entry in pipelines_with_assets.items():
        pipeline = all_pipelines.get(name)
        if pipeline is None:
            continue

        max_size_mb = entry.get("max_asset_size_mb")
        # Use member-scoped dist dir to avoid collisions
        dist_subdir = f"{member_name}/{name}" if member_name else name
        dist_dir = os.path.join(project_dir, ".rlsbl", "dist", dist_subdir)

        label = f"'{member_name}/{name}'" if member_name else f"'{name}'"

        if dry_run:
            log(f"Would build and upload assets for pipeline {label} to release {tag}")
            continue

        artifacts = []
        if entry.get("assets"):
            artifacts.extend(pipeline.build_assets(project_dir, new_version, dist_dir, ctx))
        if entry.get("custom_assets"):
            artifacts.extend(pipeline.build_custom_assets(dist_dir))

        if not artifacts:
            log(f"No artifacts produced for pipeline {label}, skipping upload.")
            continue

        # Prefix artifacts with member name for deterministic naming
        if member_name:
            artifacts = [_prefix_artifact(a, member_name) for a in artifacts]

        # Size check
        if max_size_mb is not None:
            max_size_bytes = max_size_mb * 1024 * 1024
            for artifact_path in artifacts:
                try:
                    file_size = os.path.getsize(artifact_path)
                except OSError:
                    continue
                if file_size > max_size_bytes:
                    file_name = os.path.basename(artifact_path)
                    actual_mb = file_size / (1024 * 1024)
                    if os.path.isdir(dist_dir):
                        shutil.rmtree(dist_dir)
                    raise ReleaseValidationError(
                        f"artifact '{file_name}' is {actual_mb:.1f}MB, "
                        f"exceeds max_asset_size_mb ({max_size_mb}MB) for pipeline {label}."
                    )

        try:
            run_gh(["release", "upload", tag] + artifacts + ["--clobber"], config=config)
            log(f"Uploaded {len(artifacts)} asset(s) for pipeline {label}")
        except Exception as e:
            print(f"Warning: asset upload failed for pipeline {label}: {e}", file=sys.stderr)

        if os.path.isdir(dist_dir):
            shutil.rmtree(dist_dir)


def upload_release_assets(tag, new_version, log, flags, *, ctx):
    """Build and upload release assets for pipelines with ``assets: true`` or ``custom_assets``.

    Handles standalone and implicit-mode projects. In releasable mode,
    the caller (``_run_release_mutating``) iterates members directly
    and calls ``_upload_assets_for_config`` per member.

    Skips silently if no pipelines have assets enabled.

    ctx: ProjectContext carrying project_root, monorepo_root, and config.
    """
    _upload_assets_for_config(
        tag, new_version, log, flags, ctx.config, str(ctx.project_root), ctx,
    )
