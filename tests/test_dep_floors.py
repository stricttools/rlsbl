"""Tests for the ``dep-floors`` check and its per-ecosystem readers.

The failure class: a release ships work that REQUIRES new behavior from a
sibling framework package, the dev lock resolves the new version so the
repo's own suite passes, but the published manifest carries no ``>=`` floor
(or a stale one) -- so a consumer installing the artifact resolves an older
framework and breaks at import time.

The check compares the DECLARED constraint (pyproject / package.json /
go.mod) against the LOCKED version (uv.lock / package-lock.json / go.mod)
for a configured set of ecosystem-internal dependencies.
"""

import json

import pytest

from rlsbl import app
from rlsbl.dep_floors import (
    CONFIG_KEY,
    evaluate_dep_floors,
    npm_floor,
    pypi_floor,
    workspace_package_names,
)

from conftest import make_ctx, workspace_toml


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _pyproject(root, deps, *, optional=None, groups=None, name="consumer"):
    lines = [
        "[project]",
        f'name = "{name}"',
        'version = "0.1.0"',
        "dependencies = [",
    ]
    lines += [f'    "{d}",' for d in deps]
    lines.append("]")
    if optional:
        lines.append("")
        lines.append("[project.optional-dependencies]")
        for extra, entries in optional.items():
            rendered = ", ".join(f'"{e}"' for e in entries)
            lines.append(f"{extra} = [{rendered}]")
    if groups:
        lines.append("")
        lines.append("[dependency-groups]")
        for group, entries in groups.items():
            rendered = ", ".join(f'"{e}"' for e in entries)
            lines.append(f"{group} = [{rendered}]")
    (root / "pyproject.toml").write_text("\n".join(lines) + "\n")


def _uv_lock(root, packages, *, project="consumer"):
    """Write a minimal uv.lock resolving *packages* (name -> version)."""
    blocks = [
        "version = 1",
        "requires-python = \">=3.11\"",
        "",
        "[[package]]",
        f'name = "{project}"',
        'version = "0.1.0"',
        'source = { editable = "." }',
    ]
    for name, version in packages.items():
        blocks += [
            "",
            "[[package]]",
            f'name = "{name}"',
            f'version = "{version}"',
            'source = { registry = "https://pypi.org/simple" }',
        ]
    (root / "uv.lock").write_text("\n".join(blocks) + "\n")


def _package_json(root, deps, *, peer=None, name="consumer"):
    doc = {"name": name, "version": "0.1.0", "dependencies": deps}
    if peer:
        doc["peerDependencies"] = peer
    (root / "package.json").write_text(json.dumps(doc, indent=2) + "\n")


def _package_lock_v3(root, packages, *, name="consumer"):
    doc = {
        "name": name,
        "lockfileVersion": 3,
        "packages": {
            "": {"name": name, "version": "0.1.0"},
        },
    }
    for dep, version in packages.items():
        doc["packages"][f"node_modules/{dep}"] = {"version": version}
    (root / "package-lock.json").write_text(json.dumps(doc, indent=2) + "\n")


def _package_lock_v1(root, packages, *, name="consumer"):
    doc = {
        "name": name,
        "lockfileVersion": 1,
        "dependencies": {
            dep: {"version": version} for dep, version in packages.items()
        },
    }
    (root / "package-lock.json").write_text(json.dumps(doc, indent=2) + "\n")


def _config(names=("strictcli",)):
    cfg = {"publish_mode": "ci", "targets": ["pypi"]}
    if names is not None:
        cfg[CONFIG_KEY] = list(names)
    return cfg


def _run(root, config):
    """Run the registered check exactly as ``rlsbl check`` would."""
    ctx = make_ctx(root, config=config)
    return app._check_defs["dep-floors"].impl(ctx)


def _text(result):
    return " ".join(p.text for p in result.problems)


# ---------------------------------------------------------------------------
# Adoption gate
# ---------------------------------------------------------------------------


class TestAdoptionGate:
    def test_skips_when_config_key_absent(self, tmp_path):
        _pyproject(tmp_path, ["strictcli"])
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config(names=None))
        assert result.status == "skip"
        assert CONFIG_KEY in result.message

    def test_empty_list_is_adopted_but_enforces_nothing(self, tmp_path):
        _pyproject(tmp_path, ["strictcli"])
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config(names=[]))
        assert result.status == "pass"

    def test_malformed_config_is_an_error(self, tmp_path):
        cfg = {"publish_mode": "ci", CONFIG_KEY: "strictcli"}
        result = _run(tmp_path, cfg)
        assert result.status == "fail"
        assert CONFIG_KEY in _text(result)

    def test_skips_on_virtual_uv_root(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.uv.workspace]\nmembers = [\"packages/*\"]\n"
        )
        result = _run(tmp_path, _config())
        assert result.status == "skip"


# ---------------------------------------------------------------------------
# pypi: pyproject.toml declared vs uv.lock locked
# ---------------------------------------------------------------------------


class TestPypi:
    def test_bare_dependency_with_resolving_lock_errors(self, tmp_path):
        """The exact shape that shipped broken: no floor, lock at 0.36.0."""
        _pyproject(tmp_path, ["strictcli"])
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        text = _text(result)
        assert "strictcli" in text
        assert "0.36.0" in text
        assert "strictcli>=0.36.0" in text

    def test_floor_at_locked_version_passes(self, tmp_path):
        _pyproject(tmp_path, ["strictcli>=0.36.0"])
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config())
        assert result.status == "pass"

    def test_floor_behind_locked_minor_errors(self, tmp_path):
        _pyproject(tmp_path, ["strictcli>=0.35.0"])
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        text = _text(result)
        assert ">=0.35.0" in text
        assert "strictcli>=0.36.0" in text

    def test_patch_drift_above_floor_passes(self, tmp_path):
        """Floors are major.minor: a patch bump in the lock is not a boundary."""
        _pyproject(tmp_path, ["strictcli>=0.36.0"])
        _uv_lock(tmp_path, {"strictcli": "0.36.4"})
        result = _run(tmp_path, _config())
        assert result.status == "pass"

    def test_major_boundary_errors(self, tmp_path):
        _pyproject(tmp_path, ["strictcli>=0.36.0"])
        _uv_lock(tmp_path, {"strictcli": "1.0.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        assert "strictcli>=1.0.0" in _text(result)

    def test_optional_dependency_bucket_is_covered(self, tmp_path):
        _pyproject(tmp_path, [], optional={"dev": ["strictcli"]})
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        assert "optional-dependencies" in _text(result)

    def test_pep_735_dependency_group_is_covered(self, tmp_path):
        """A dev-group internal dep still needs a floor.

        The reader used to look only at ``[project].dependencies`` and
        ``[project].optional-dependencies``, so an internal dependency
        declared in a PEP 735 ``[dependency-groups]`` table was silently
        DROPPED -- no verdict at all, however far behind its floor was.
        A test-infrastructure dependency is exactly where that shape lives.
        """
        _pyproject(tmp_path, [], groups={"dev": ["stricttest"]})
        _uv_lock(tmp_path, {"stricttest": "0.4.0"})
        result = _run(tmp_path, _config(names=("stricttest",)))
        assert result.status == "fail"
        text = _text(result)
        assert "[dependency-groups].dev" in text
        assert "stricttest>=0.4.0" in text

    def test_dependency_group_floor_at_locked_version_passes(self, tmp_path):
        _pyproject(tmp_path, [], groups={"dev": ["stricttest>=0.4.0"]})
        _uv_lock(tmp_path, {"stricttest": "0.4.0"})
        result = _run(tmp_path, _config(names=("stricttest",)))
        assert result.status == "pass"

    def test_runtime_declaration_wins_over_a_dependency_group(self, tmp_path):
        """A consumer resolves the RUNTIME floor; that is the one to report."""
        _pyproject(
            tmp_path, ["strictcli>=0.35.0"], groups={"dev": ["strictcli"]},
        )
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        assert "[project].dependencies" in _text(result)

    def test_transitive_dep_not_declared_is_ignored(self, tmp_path):
        """A name only in the lock is transitive -- not this project's floor."""
        _pyproject(tmp_path, ["requests"])
        _uv_lock(tmp_path, {"strictcli": "0.36.0", "requests": "2.0.0"})
        result = _run(tmp_path, _config())
        assert result.status == "pass"

    def test_non_internal_dep_is_ignored(self, tmp_path):
        _pyproject(tmp_path, ["requests"])
        _uv_lock(tmp_path, {"requests": "2.31.0"})
        result = _run(tmp_path, _config())
        assert result.status == "pass"

    def test_name_normalization(self, tmp_path):
        """PEP 503 normalization: My_Lib in the manifest, my-lib in config."""
        _pyproject(tmp_path, ["My_Lib"])
        _uv_lock(tmp_path, {"my-lib": "2.1.0"})
        result = _run(tmp_path, _config(names=["my-lib"]))
        assert result.status == "fail"
        assert "my-lib>=2.1.0" in _text(result)

    def test_extras_marker_and_environment_marker_are_stripped(self, tmp_path):
        _pyproject(tmp_path, ['strictcli[cli]>=0.36.0 ; python_version >= "3.11"'])
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config())
        assert result.status == "pass"

    def test_path_dependency_is_skipped(self, tmp_path):
        _pyproject(tmp_path, ["strictcli @ file:///opt/strictcli"])
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config())
        assert result.status == "pass"

    def test_no_lockfile_reports_nothing_to_compare(self, tmp_path):
        _pyproject(tmp_path, ["strictcli"])
        result = _run(tmp_path, _config())
        assert result.status == "pass"
        assert "uv.lock" in result.message

    def test_equality_pin_counts_as_a_floor(self, tmp_path):
        _pyproject(tmp_path, ["strictcli==0.36.0"])
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config())
        assert result.status == "pass"

    def test_upper_bound_only_has_no_floor(self, tmp_path):
        _pyproject(tmp_path, ["strictcli<1"])
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        assert "strictcli>=0.36.0" in _text(result)


# ---------------------------------------------------------------------------
# npm: package.json declared vs package-lock.json locked
# ---------------------------------------------------------------------------


class TestNpm:
    def test_wildcard_range_with_resolving_lock_errors(self, tmp_path):
        _package_json(tmp_path, {"strictcli": "*"})
        _package_lock_v3(tmp_path, {"strictcli": "1.4.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        text = _text(result)
        assert '"strictcli": ">=1.4.0"' in text

    def test_caret_range_at_locked_version_passes(self, tmp_path):
        _package_json(tmp_path, {"strictcli": "^1.4.0"})
        _package_lock_v3(tmp_path, {"strictcli": "1.4.2"})
        result = _run(tmp_path, _config())
        assert result.status == "pass"

    def test_caret_range_behind_locked_minor_errors(self, tmp_path):
        _package_json(tmp_path, {"strictcli": "^1.3.0"})
        _package_lock_v3(tmp_path, {"strictcli": "1.4.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        assert '"strictcli": ">=1.4.0"' in _text(result)

    def test_lockfile_version_1_format(self, tmp_path):
        _package_json(tmp_path, {"strictcli": "^1.3.0"})
        _package_lock_v1(tmp_path, {"strictcli": "1.4.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        assert "1.4.0" in _text(result)

    def test_peer_dependencies_are_covered(self, tmp_path):
        _package_json(tmp_path, {}, peer={"strictcli": "^1.3.0"})
        _package_lock_v3(tmp_path, {"strictcli": "1.4.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"
        assert "peerDependencies" in _text(result)

    def test_workspace_protocol_is_skipped(self, tmp_path):
        _package_json(tmp_path, {"strictcli": "workspace:*"})
        _package_lock_v3(tmp_path, {"strictcli": "1.4.0"})
        result = _run(tmp_path, _config())
        assert result.status == "pass"

    def test_no_lockfile_reports_nothing_to_compare(self, tmp_path):
        _package_json(tmp_path, {"strictcli": "*"})
        result = _run(tmp_path, _config())
        assert result.status == "pass"
        assert "package-lock.json" in result.message


# ---------------------------------------------------------------------------
# go: go.mod declares its own minimums
# ---------------------------------------------------------------------------


class TestGo:
    def test_go_is_automatically_satisfied(self, tmp_path):
        (tmp_path / "go.mod").write_text(
            "module example.com/consumer\n\ngo 1.23\n\n"
            "require github.com/smm-h/strictcli v0.36.0\n"
        )
        result = _run(tmp_path, _config(names=["github.com/smm-h/strictcli"]))
        assert result.status == "pass"
        assert "go.mod" in result.message

    def test_go_alongside_a_broken_pypi_manifest_still_errors(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/c\n\ngo 1.23\n")
        _pyproject(tmp_path, ["strictcli"])
        _uv_lock(tmp_path, {"strictcli": "0.36.0"})
        result = _run(tmp_path, _config())
        assert result.status == "fail"


# ---------------------------------------------------------------------------
# Internal-dep identification
# ---------------------------------------------------------------------------


class TestInternalNames:
    def test_workspace_siblings_extend_the_enforced_set(self, tmp_path):
        ws = tmp_path / ".rlsbl-monorepo"
        ws.mkdir()
        (ws / "workspace.toml").write_text(
            workspace_toml("[[projects]]\n"
            'name = "core"\n'
            'path = "packages/core"\n'
            "\n"
            "[[projects]]\n"
            'name = "cli"\n'
            'path = "packages/cli"\n'
            'registry_name = "my-cli"\n')
        )
        assert workspace_package_names(tmp_path) == {"core", "my-cli"}

    def test_no_workspace_yields_no_names(self, tmp_path):
        assert workspace_package_names(tmp_path) == set()
        assert workspace_package_names(None) == set()

    def test_sibling_name_is_enforced_without_being_listed(self, tmp_path):
        pkg = tmp_path / "packages" / "cli"
        pkg.mkdir(parents=True)
        ws = tmp_path / ".rlsbl-monorepo"
        ws.mkdir()
        (ws / "workspace.toml").write_text(
            workspace_toml("[[projects]]\n"
            'name = "core"\n'
            'path = "packages/core"\n')
        )
        _pyproject(pkg, ["core"])
        _uv_lock(pkg, {"core": "0.4.0"})
        verdict = evaluate_dep_floors(
            _config(names=[]),
            str(pkg),
            workspace_names=workspace_package_names(tmp_path),
        )
        assert verdict.adopted
        assert not verdict.ok
        assert "core>=0.4.0" in " ".join(verdict.problems)


# ---------------------------------------------------------------------------
# Floor parsers (unit level)
# ---------------------------------------------------------------------------


class TestFloorParsers:
    @pytest.mark.parametrize(
        "spec,expected",
        [
            (">=0.36.0", ("floor", (0, 36))),
            (">=0.36", ("floor", (0, 36))),
            ("==1.2.3", ("floor", (1, 2))),
            ("~=1.2.3", ("floor", (1, 2))),
            (">1.2.3", ("floor", (1, 2))),
            (">=1.0,<2", ("floor", (1, 0))),
            ("<2", ("none", None)),
            ("", ("none", None)),
            ("!=1.0", ("none", None)),
        ],
    )
    def test_pypi_floor(self, spec, expected):
        assert pypi_floor(spec) == expected

    @pytest.mark.parametrize(
        "rng,expected",
        [
            ("^1.4.0", ("floor", (1, 4))),
            ("~1.4.2", ("floor", (1, 4))),
            (">=1.4.0", ("floor", (1, 4))),
            ("1.4.0", ("floor", (1, 4))),
            ("=1.4.0", ("floor", (1, 4))),
            (">=1.4.0 <2.0.0", ("floor", (1, 4))),
            ("*", ("none", None)),
            ("", ("none", None)),
            ("^1.0.0 || ^2.0.0", ("none", None)),
            ("workspace:*", ("skip", None)),
            ("file:../core", ("skip", None)),
            ("git+https://example.com/x.git", ("skip", None)),
        ],
    )
    def test_npm_floor(self, rng, expected):
        assert npm_floor(rng) == expected
