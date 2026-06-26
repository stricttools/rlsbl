"""Publishing helpers: selfdoc blog post generation, asset uploads, stale dep advisory."""

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


def _run_selfdoc_post_generate(flags, *, project_dir=None, release_config=None,
                                new_version=None, current_version=None,
                                bump_type=None, changelog_entry=None, tag=None):
    """Generate a blog post via selfdoc during release.

    Called when release_config.blog is True and selfdoc.json exists.
    Writes the changelog entry to a temp file and invokes
    ``selfdoc post generate --from-release`` with all release metadata.

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
        print(f"Would run: selfdoc post generate --from-release --version {new_version}")
        return True

    if not require_tool("selfdoc", fatal=False):
        print(
            "Note: blog = true but selfdoc is not installed. Skipping blog post generation."
        )
        return True

    print("Generating blog post via selfdoc...")

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
        cmd = ["selfdoc", "post", "generate", "--from-release"]
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

        # Body file (optional)
        blog_body_path = os.path.join(check_dir, ".rlsbl", "releases", "unreleased.md")
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
            f"selfdoc post generate failed (exit code {e.returncode})."
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


def upload_release_assets(tag, new_version, log, flags, *, ctx):
    """Build and upload release assets for pipelines with ``assets: true`` or ``custom_assets``.

    For each pipeline that has assets enabled:
    1. Create a dist directory under ``.rlsbl/dist/<pipeline_name>/``
    2. Call ``pipeline.build_assets()`` and/or ``pipeline.build_custom_assets()``
    3. Check each artifact against ``max_asset_size_mb``
    4. Upload via ``gh release upload``
    5. Clean up the dist directory

    Skips silently if no pipelines have assets enabled.

    ctx: ProjectContext carrying project_root, monorepo_root, and config.
    """
    from . import load_pipelines, run, run_gh

    project_dir = str(ctx.project_root)
    config = ctx.config

    pipelines_cfg = config.get("pipelines", {})
    if not isinstance(pipelines_cfg, dict):
        return

    # Find pipelines with assets or custom_assets
    pipelines_with_assets = {}
    for name, entry in pipelines_cfg.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("assets") or entry.get("custom_assets"):
            pipelines_with_assets[name] = entry

    if not pipelines_with_assets:
        return

    dry_run = flags.get("dry-run", False)

    # Load pipeline instances for asset building
    all_pipelines = load_pipelines(config)

    for name, entry in pipelines_with_assets.items():
        pipeline = all_pipelines.get(name)
        if pipeline is None:
            continue

        max_size_mb = entry.get("max_asset_size_mb")
        dist_dir = os.path.join(project_dir, ".rlsbl", "dist", name)

        if dry_run:
            log(f"Would build and upload assets for pipeline '{name}' to release {tag}")
            continue

        # Build standard assets
        artifacts = []
        if entry.get("assets"):
            artifacts.extend(pipeline.build_assets(project_dir, new_version, dist_dir, ctx))

        # Build custom assets
        if entry.get("custom_assets"):
            artifacts.extend(pipeline.build_custom_assets(dist_dir))

        if not artifacts:
            log(f"No artifacts produced for pipeline '{name}', skipping upload.")
            continue

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
                    # Clean up dist before aborting
                    if os.path.isdir(dist_dir):
                        shutil.rmtree(dist_dir)
                    raise ReleaseValidationError(
                        f"artifact '{file_name}' is {actual_mb:.1f}MB, "
                        f"exceeds max_asset_size_mb ({max_size_mb}MB) for pipeline '{name}'."
                    )

        # Upload
        try:
            run_gh(["release", "upload", tag] + artifacts + ["--clobber"], config=config)
            log(f"Uploaded {len(artifacts)} asset(s) for pipeline '{name}'")
        except Exception as e:
            print(f"Warning: asset upload failed for pipeline '{name}': {e}", file=sys.stderr)

        # Clean up dist directory
        if os.path.isdir(dist_dir):
            shutil.rmtree(dist_dir)
