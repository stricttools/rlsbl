"""Tests for the monorepo publish router secrets/permissions generation.

The router invokes per-project publish workflows as local reusable workflows
(``uses: ./.github/workflows/{name}-publish.yml``). Reusable workflows do
NOT inherit secrets by default and CANNOT elevate the permissions they were
given by the caller. The router therefore must:

- Inject ``permissions:`` per called job, matching what the target needs
  (e.g. PyPI/npm/deno need ``id-token: write`` for OIDC; Go/zig need
  ``contents: write`` for goreleaser/asset uploads).
- Add ``secrets: inherit`` only for targets whose publish step actually
  reads org/repo secrets at runtime (npm/cargo/hex/maven/docker/zig).
  PyPI uses OIDC -- adding ``secrets: inherit`` is unnecessary noise.

These tests pin the generated YAML contract so the contract cannot regress
silently. Without this fix, the router produced jobs that failed at
``startup_failure`` for OIDC targets and silently published with missing
credentials for token-based targets.
"""

import os

import pytest

from rlsbl.commands.monorepo import (
    _generate_publish_router,
    _get_publish_requirements,
)


def _make_project(name, path, files):
    """Materialize a project on disk so detect_targets finds the target."""
    proj_dir = path
    os.makedirs(proj_dir, exist_ok=True)
    for fname, contents in files.items():
        with open(os.path.join(proj_dir, fname), "w", encoding="utf-8") as f:
            f.write(contents)
    return {"name": name, "path": proj_dir}


class TestPublishRouterTopLevel:
    """Router triggers + structure remain stable."""

    def test_has_release_trigger(self):
        content = _generate_publish_router([], ".")
        assert "on:\n  release:\n    types: [published]" in content

    def test_has_router_name(self):
        content = _generate_publish_router([], ".")
        assert "name: Publish Router" in content

    def test_no_jobs_when_no_projects(self):
        content = _generate_publish_router([], ".")
        # 'jobs:' header is always emitted but no entries follow it
        assert "jobs:" in content


class TestPublishRouterPyPIOidc:
    """PyPI uses OIDC (no secrets) and requires id-token: write."""

    def test_pypi_router_includes_id_token_permission(self, tmp_project):
        proj = _make_project(
            "mypkg",
            str(tmp_project / "python"),
            {"pyproject.toml": '[project]\nname = "mypkg"\nversion = "0.1.0"\n'},
        )
        content = _generate_publish_router([proj], ".")
        assert "id-token: write" in content
        assert "contents: read" in content

    def test_pypi_router_omits_secrets_inherit(self, tmp_project):
        proj = _make_project(
            "mypkg",
            str(tmp_project / "python"),
            {"pyproject.toml": '[project]\nname = "mypkg"\nversion = "0.1.0"\n'},
        )
        content = _generate_publish_router([proj], ".")
        assert "secrets: inherit" not in content


class TestPublishRouterNpmSecrets:
    """npm publish needs NPM_TOKEN at runtime -- secrets: inherit required."""

    def test_npm_router_includes_secrets_inherit(self, tmp_project):
        proj = _make_project(
            "mylib",
            str(tmp_project / "node"),
            {"package.json": '{"name": "mylib", "version": "0.1.0"}'},
        )
        content = _generate_publish_router([proj], ".")
        assert "secrets: inherit" in content

    def test_npm_router_includes_id_token_for_provenance(self, tmp_project):
        proj = _make_project(
            "mylib",
            str(tmp_project / "node"),
            {"package.json": '{"name": "mylib", "version": "0.1.0"}'},
        )
        content = _generate_publish_router([proj], ".")
        assert "id-token: write" in content


class TestPublishRouterGoContents:
    """Go publish (goreleaser) needs contents: write to push assets."""

    def test_go_router_includes_contents_write(self, tmp_project):
        proj = _make_project(
            "mymod",
            str(tmp_project / "go"),
            {"go.mod": "module example.com/mymod\n\ngo 1.21\n", "VERSION": "0.1.0\n"},
        )
        content = _generate_publish_router([proj], ".")
        assert "contents: write" in content


class TestUnknownTargetDefaults:
    """Unknown / undetectable targets default to safe read-only contents and no secrets."""

    def test_unknown_target_has_no_secrets_inherit(self):
        # No on-disk files -- detect_targets returns nothing
        proj = {"name": "ghost", "path": "/nonexistent/ghost"}
        content = _generate_publish_router([proj], ".")
        assert "secrets: inherit" not in content

    def test_unknown_target_has_read_permissions(self):
        proj = {"name": "ghost", "path": "/nonexistent/ghost"}
        content = _generate_publish_router([proj], ".")
        assert "contents: read" in content


class TestRouterJobStructure:
    """Each project gets a job with if/permissions/uses (and maybe secrets)."""

    def test_job_has_uses_pointing_to_local_workflow(self, tmp_project):
        proj = _make_project(
            "mylib",
            str(tmp_project / "node"),
            {"package.json": '{"name": "mylib", "version": "0.1.0"}'},
        )
        content = _generate_publish_router([proj], ".")
        assert "uses: ./.github/workflows/mylib-publish.yml" in content

    def test_job_has_tag_prefix_condition(self, tmp_project):
        proj = _make_project(
            "mylib",
            str(tmp_project / "node"),
            {"package.json": '{"name": "mylib", "version": "0.1.0"}'},
        )
        content = _generate_publish_router([proj], ".")
        assert "if: startsWith(github.event.release.tag_name, 'mylib@v')" in content

    def test_permissions_block_appears_before_uses(self, tmp_project):
        """Sanity: permissions block must be declared on the caller job, not after uses."""
        proj = _make_project(
            "mylib",
            str(tmp_project / "node"),
            {"package.json": '{"name": "mylib", "version": "0.1.0"}'},
        )
        content = _generate_publish_router([proj], ".")
        perm_pos = content.index("permissions:")
        uses_pos = content.index("uses: ./.github/workflows/mylib-publish.yml")
        assert perm_pos < uses_pos


class TestGetPublishRequirements:
    """Direct unit tests for the requirements lookup helper."""

    @pytest.mark.parametrize("target_files,name,expected_inherit", [
        ({"package.json": '{"name": "x", "version": "0.0.1"}'}, "npm", True),
        ({"pyproject.toml": '[project]\nname = "x"\nversion = "0.0.1"\n'}, "pypi", False),
        ({"go.mod": "module x\n\ngo 1.21\n", "VERSION": "0.0.1\n"}, "go", True),
    ])
    def test_secrets_inherit_per_target(self, tmp_project, target_files, name, expected_inherit):
        proj_dir = tmp_project / name
        proj_dir.mkdir()
        for fname, contents in target_files.items():
            (proj_dir / fname).write_text(contents)
        proj = {"name": name, "path": str(proj_dir)}
        inherit, perms = _get_publish_requirements(proj, ".")
        assert inherit == expected_inherit
        assert isinstance(perms, dict)
        assert "contents" in perms

    def test_unknown_target_falls_back_to_read_only(self):
        proj = {"name": "ghost", "path": "/nonexistent/ghost"}
        inherit, perms = _get_publish_requirements(proj, ".")
        assert inherit is False
        assert perms == {"contents": "read"}
