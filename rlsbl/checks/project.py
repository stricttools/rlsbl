"""Project checks (tag: project).

Checks: lock, version-consistency, name-consistency, license-consistency,
description-consistency, private-hook-stale, config-schema, license-file,
private-publish-workflow, npm-private-mismatch, target-version-readable,
selfdoc-version-drift.
"""

import json
import os

from strictcli import CheckResult

from ..errors import ConfigError


def register_project_checks(app):
    """Register project-tag checks on *app*."""

    @app.check("lock")
    def check_lock(ctx):
        """Detect stale lock files."""
        from ..lock import is_stale

        root_str = str(ctx.project_root)
        stale_paths = []
        if is_stale(lock_path=os.path.join(root_str, ".rlsbl", "lock"), project_root=ctx.project_root):
            stale_paths.append(".rlsbl/lock")
        if is_stale(lock_path=os.path.join(root_str, ".rlsbl-monorepo", "lock"), project_root=ctx.project_root):
            stale_paths.append(".rlsbl-monorepo/lock")

        if stale_paths:
            return CheckResult("warn", f"stale lock file exists at {', '.join(stale_paths)}")
        return CheckResult("pass", "no lock file")

    @app.check("version-consistency")
    def check_version_consistency(ctx):
        """All detected targets must report the same version."""
        from ..targets import TARGETS, detect_targets

        target_entries = detect_targets(str(ctx.project_root))
        if not target_entries:
            return CheckResult("warn", "no targets detected")

        versions = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                v = target.read_version(path)
                versions[name] = v
            except Exception:
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
                except (OSError, json.JSONDecodeError):
                    versions["selfdoc"] = None

        unique = set(v for v in versions.values() if v is not None)
        if len(unique) == 0:
            return CheckResult("warn", "no targets reported a version")
        if len(unique) > 1:
            detail = ", ".join(f"{n}={v}" for n, v in versions.items() if v is not None)
            return CheckResult("fail", f"version mismatch: {detail}")

        version = unique.pop()
        return CheckResult("pass", f"{version} across {len(versions)} target(s)")

    @app.check("name-consistency")
    def check_name_consistency(ctx):
        """All detected targets must report the same package name."""
        from ..targets import TARGETS, detect_targets
        from ..targets.utils import normalize_go, normalize_npm, normalize_pypi

        def _normalize_name(target_name, raw_name):
            normalizers = {
                "npm": normalize_npm,
                "pypi": normalize_pypi,
                "go": normalize_go,
            }
            normalizer = normalizers.get(target_name, str.lower)
            return normalizer(raw_name)

        target_entries = detect_targets(str(ctx.project_root))
        if not target_entries:
            return CheckResult("warn", "no targets detected")

        names = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                n = target.read_name(path, ctx=ctx)
                names[name] = n
            except Exception:
                names[name] = None

        have_name = {k: v for k, v in names.items() if v is not None}
        if not have_name:
            return CheckResult("warn", "no targets reported a name")

        missing = [k for k, v in names.items() if v is None]
        normalized = {k: _normalize_name(k, v) for k, v in have_name.items()}
        unique = set(normalized.values())

        if len(unique) == 1:
            raw_name = next(iter(have_name.values()))
            msg = f"{raw_name} across {len(target_entries)} target(s)"
            if missing:
                msg += f" (no name from: {', '.join(missing)})"
            return CheckResult("pass", msg)

        detail = ", ".join(f"{k}={v}" for k, v in have_name.items())
        return CheckResult("warn", f"name mismatch: {detail}")

    @app.check("license-consistency")
    def check_license_consistency(ctx):
        """All detected targets must report the same license."""
        from ..targets import TARGETS, detect_targets

        target_entries = detect_targets(str(ctx.project_root))
        if not target_entries:
            return CheckResult("pass", "no targets declare a license")

        licenses = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                meta = target.read_metadata(path)
                if "license" in meta:
                    licenses[name] = meta["license"]
            except Exception:
                pass

        if len(licenses) == 0:
            return CheckResult("pass", "no targets declare a license")
        if len(licenses) < 2:
            return CheckResult("pass", f"only {len(licenses)} target(s) declare a license")

        unique = set(v.lower() for v in licenses.values())
        if len(unique) == 1:
            license_val = next(iter(licenses.values()))
            return CheckResult("pass", f"{license_val} across {len(licenses)} target(s)")

        detail = ", ".join(f"{k}={v}" for k, v in licenses.items())
        return CheckResult("warn", f"license mismatch: {detail}")

    @app.check("description-consistency")
    def check_description_consistency(ctx):
        """All detected targets must report the same description."""
        from ..targets import TARGETS, detect_targets

        target_entries = detect_targets(str(ctx.project_root))
        if not target_entries:
            return CheckResult("pass", "no targets declare a description")

        descriptions = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                meta = target.read_metadata(path)
                if "description" in meta:
                    descriptions[name] = meta["description"]
            except Exception:
                pass

        if len(descriptions) == 0:
            return CheckResult("pass", "no targets declare a description")
        if len(descriptions) < 2:
            return CheckResult("pass", f"only {len(descriptions)} target(s) declare a description")

        unique = set(descriptions.values())
        if len(unique) == 1:
            desc_val = next(iter(descriptions.values()))
            return CheckResult("pass", f"{desc_val} across {len(descriptions)} target(s)")

        detail = ", ".join(f"{k}={v}" for k, v in descriptions.items())
        return CheckResult("warn", f"description mismatch: {detail}")

    @app.check("private-hook-stale")
    def check_private_hook_stale(ctx):
        """Detect legacy private asset upload code in post-release hook."""
        hook_path = os.path.join(str(ctx.project_root), ".rlsbl", "hooks", "post-release.sh")
        if not os.path.exists(hook_path):
            return CheckResult("pass", "no post-release hook")

        with open(hook_path, "r", encoding="utf-8") as f:
            content = f.read()

        # The old private hook template had this distinctive comment line
        if "Post-release hook for private repositories" in content:
            return CheckResult(
                "fail",
                "Post-release hook contains legacy private asset upload code. "
                "Asset upload is now a built-in release step. "
                "Run `rlsbl scaffold` to get the standard hook template.",
            )
        return CheckResult("pass", "no legacy private hook code")

    @app.check("config-schema")
    def check_config_schema(ctx):
        """Validate .rlsbl/config.json schema: private key and pipelines config."""
        config = ctx.config
        errors = []

        if "private" not in config:
            errors.append('"private" key missing from .rlsbl/config.json')

        # Validate pipelines config if present
        from ..config import validate_pipelines_config
        try:
            validate_pipelines_config(config)
        except ConfigError as e:
            errors.append(str(e))

        if errors:
            return CheckResult("fail", f"{len(errors)} config error(s)", details=errors)
        return CheckResult("pass", "config schema valid")

    @app.check("license-file")
    def check_license_file(ctx):
        """LICENSE file must exist, be non-empty, and have no template variables."""
        import re as _re

        license_path = os.path.join(str(ctx.project_root), "LICENSE")
        if not os.path.exists(license_path):
            return CheckResult("fail", "LICENSE file not found in project root")

        try:
            size = os.path.getsize(license_path)
        except OSError:
            return CheckResult("fail", "cannot read LICENSE file")

        if size == 0:
            return CheckResult("fail", "LICENSE file is empty")

        with open(license_path, "r", encoding="utf-8") as f:
            content = f.read()

        template_vars = _re.findall(r"\{\{\w+(?:\.\w+)*\}\}", content)
        if template_vars:
            return CheckResult(
                "fail",
                f"LICENSE contains unreplaced template variable(s): {', '.join(template_vars)}",
            )

        return CheckResult("pass", "LICENSE file valid")

    @app.check("private-publish-workflow")
    def check_private_publish_workflow(ctx):
        """Private repos must not have publish workflows."""
        if not ctx.config.get("private"):
            return CheckResult("pass", "not a private repo")

        import glob

        root_str = str(ctx.project_root)
        wf_dir = os.path.join(root_str, ".github", "workflows")
        if not os.path.isdir(wf_dir):
            return CheckResult("pass", "no .github/workflows/ directory")

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
            return CheckResult(
                "fail",
                f"private repo has publish workflow(s): {', '.join(sorted(publish_files))}",
            )
        return CheckResult("pass", "no publish workflows in private repo")

    @app.check("npm-private-mismatch")
    def check_npm_private_mismatch(ctx):
        """package.json private:true must not contradict config private:false."""
        root_str = str(ctx.project_root)
        pkg_path = os.path.join(root_str, "package.json")
        if not os.path.exists(pkg_path):
            return CheckResult("skip", "no package.json")

        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return CheckResult("skip", "cannot read package.json")

        npm_private = pkg.get("private", False)
        config_private = ctx.config.get("private")

        if npm_private is True and config_private is False:
            return CheckResult(
                "fail",
                'package.json has "private": true but .rlsbl/config.json has "private": false',
            )
        return CheckResult("pass", "npm private flag consistent with config")

    @app.check("target-version-readable")
    def check_target_version_readable(ctx):
        """Every detected target must be able to read its version without error."""
        from ..targets import TARGETS, detect_targets

        target_entries = detect_targets(str(ctx.project_root))
        if not target_entries:
            return CheckResult("skip", "no targets detected")

        errors = []
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                target.read_version(path)
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        if errors:
            return CheckResult(
                "fail",
                f"{len(errors)} target(s) cannot read version",
                details=errors,
            )
        return CheckResult(
            "pass",
            f"all {len(target_entries)} target(s) version readable",
        )

    @app.check("selfdoc-version-drift")
    def check_selfdoc_version_drift(ctx):
        """selfdoc.json version must match the primary target's version."""
        root_str = str(ctx.project_root)
        selfdoc_path = os.path.join(root_str, "selfdoc.json")
        if not os.path.exists(selfdoc_path):
            return CheckResult("skip", "no selfdoc.json")

        try:
            with open(selfdoc_path, "r", encoding="utf-8") as f:
                selfdoc_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return CheckResult("skip", "cannot read selfdoc.json")

        selfdoc_version = selfdoc_data.get("version")
        if selfdoc_version is None:
            return CheckResult("skip", "selfdoc.json has no version field")

        from ..targets import TARGETS, detect_targets

        target_entries = detect_targets(root_str)
        if not target_entries:
            return CheckResult("skip", "no targets detected")

        first_name, first_path = target_entries[0]
        target = TARGETS[first_name]
        try:
            primary_version = target.read_version(first_path)
        except Exception:
            return CheckResult("skip", "cannot read primary target version")

        if primary_version is None:
            return CheckResult("skip", "primary target reports no version")

        if selfdoc_version != primary_version:
            return CheckResult(
                "fail",
                f"selfdoc.json version ({selfdoc_version}) != "
                f"primary target {first_name} version ({primary_version})",
            )
        return CheckResult("pass", f"selfdoc.json version matches ({selfdoc_version})")
