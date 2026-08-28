"""Tests for the ``dep-locks`` check and its per-ecosystem comparisons.

The failure class: a dependency is added, removed or re-constrained in the
manifest and the lockfile is never regenerated. ``uv sync`` / ``npm ci`` keep
installing the old resolution, ``dep-floors`` compares against a lock that no
longer describes the manifest, and the drift finally surfaces as an unrelated
diff in a release commit.

Every comparison here is structural and offline: no package manager runs.
"""

import json

import pytest

from rlsbl import app
from rlsbl.dep_locks import evaluate_dep_locks, normalize_specifier, parse_go_mod

from conftest import make_ctx


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _pyproject(root, deps, *, optional=None, groups=None, name="consumer",
               version="0.1.0", sources=None):
    lines = ["[project]", f'name = "{name}"']
    if version is not None:
        lines.append(f'version = "{version}"')
    lines.append("dependencies = [")
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
    if sources:
        lines.append("")
        lines.append("[tool.uv.sources]")
        for dep, spec in sources.items():
            lines.append(f"{dep} = {spec}")
    (root / "pyproject.toml").write_text("\n".join(lines) + "\n")


def _uv_lock(root, *, requires=(), dev=None, name="consumer", version="0.1.0",
             source_path=".", metadata=True):
    """A uv.lock whose entry for this project records *requires* / *dev*.

    ``metadata=False`` omits the ``[package.metadata]`` table entirely, which
    is what uv writes for a project that declares no requirement at all.
    """
    lines = [
        "version = 1",
        'requires-python = ">=3.11"',
        "",
        "[[package]]",
        f'name = "{name}"',
        f'version = "{version}"',
        f'source = {{ editable = "{source_path}" }}',
    ]
    if metadata:
        lines += ["", "[package.metadata]", "requires-dist = ["]
        for entry in requires:
            lines.append(f"    {_inline(entry)},")
        lines.append("]")
        for group, entries in (dev or {}).items():
            lines.append("")
            lines.append("[package.metadata.requires-dev]")
            lines.append(f"{group} = [")
            for entry in entries:
                lines.append(f"    {_inline(entry)},")
            lines.append("]")
    (root / "uv.lock").write_text("\n".join(lines) + "\n")


def _inline(entry):
    """Render one requires-dist entry.

    A bare name, a ``(name, specifier)`` pair, or a ``{key: value}`` mapping
    rendered verbatim -- which is how a source-backed entry
    (``{ name = "x", editable = "x" }``) is written.
    """
    if isinstance(entry, dict):
        body = ", ".join(f'{k} = "{v}"' for k, v in entry.items())
        return f"{{ {body} }}"
    if isinstance(entry, tuple):
        dep, specifier = entry
        return f'{{ name = "{dep}", specifier = "{specifier}" }}'
    return f'{{ name = "{entry}" }}'


def _package_json(root, deps=None, *, dev=None, name="consumer", version="0.1.0"):
    doc = {"name": name, "version": version}
    if deps:
        doc["dependencies"] = deps
    if dev:
        doc["devDependencies"] = dev
    (root / "package.json").write_text(json.dumps(doc, indent=2) + "\n")


def _package_lock_v3(root, deps=None, *, dev=None, name="consumer",
                     version="0.1.0"):
    entry = {"name": name, "version": version}
    if deps:
        entry["dependencies"] = deps
    if dev:
        entry["devDependencies"] = dev
    doc = {"name": name, "lockfileVersion": 3, "packages": {"": entry}}
    for dep in list((deps or {})) + list((dev or {})):
        doc["packages"][f"node_modules/{dep}"] = {"version": "1.0.0"}
    (root / "package-lock.json").write_text(json.dumps(doc, indent=2) + "\n")


def _package_lock_v1(root, resolved, *, name="consumer", version="0.1.0"):
    doc = {
        "name": name,
        "version": version,
        "lockfileVersion": 1,
        "dependencies": {dep: {"version": v} for dep, v in resolved.items()},
    }
    (root / "package-lock.json").write_text(json.dumps(doc, indent=2) + "\n")


def _run(root):
    """Run the registered check exactly as ``rlsbl check`` would."""
    ctx = make_ctx(root, config={"publish_mode": "ci"})
    return app._check_defs["dep-locks"].impl(ctx)


def _text(result):
    return " ".join(p.text for p in result.problems)


# ---------------------------------------------------------------------------
# Applicability
# ---------------------------------------------------------------------------


class TestApplicability:
    def test_skips_with_no_manifest_at_all(self, tmp_path):
        result = _run(tmp_path)
        assert result.status == "skip"

    def test_no_uv_lock_is_a_note_not_an_error(self, tmp_path):
        _pyproject(tmp_path, ["requests"])
        result = _run(tmp_path)
        assert result.status == "pass"
        assert "uv.lock" in result.message

    def test_skips_on_virtual_uv_root(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
        )
        result = _run(tmp_path)
        assert result.status == "skip"


# ---------------------------------------------------------------------------
# pypi: pyproject.toml vs uv.lock
# ---------------------------------------------------------------------------


class TestPypi:
    def test_a_lock_that_resolves_the_manifest_passes(self, tmp_path):
        _pyproject(tmp_path, ["requests>=2.0"])
        _uv_lock(tmp_path, requires=[("requests", ">=2.0")])
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_dependency_added_after_the_lock_errors(self, tmp_path):
        """The shape that ships stale: a new requirement the lock never saw."""
        _pyproject(tmp_path, ["requests>=2.0", "tomlkit>=0.12"])
        _uv_lock(tmp_path, requires=[("requests", ">=2.0")])
        result = _run(tmp_path)
        assert result.status == "fail"
        text = _text(result)
        assert "tomlkit" in text
        assert "uv lock" in text

    def test_a_dependency_removed_after_the_lock_errors(self, tmp_path):
        _pyproject(tmp_path, ["requests>=2.0"])
        _uv_lock(
            tmp_path, requires=[("requests", ">=2.0"), ("tomlkit", ">=0.12")],
        )
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "tomlkit" in _text(result)

    def test_a_changed_constraint_errors(self, tmp_path):
        _pyproject(tmp_path, ["requests>=2.31"])
        _uv_lock(tmp_path, requires=[("requests", ">=2.0")])
        result = _run(tmp_path)
        assert result.status == "fail"
        text = _text(result)
        assert ">=2.31" in text and ">=2.0" in text

    def test_clause_order_is_not_a_difference(self, tmp_path):
        _pyproject(tmp_path, ["requests>=2.0,<3"])
        _uv_lock(tmp_path, requires=[("requests", "<3,>=2.0")])
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_version_bump_the_lock_missed_errors(self, tmp_path):
        _pyproject(tmp_path, ["requests>=2.0"], version="0.2.0")
        _uv_lock(tmp_path, requires=[("requests", ">=2.0")], version="0.1.0")
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "0.2.0" in _text(result)

    def test_optional_dependencies_are_compared(self, tmp_path):
        """uv folds extras into requires-dist, so an extra is in scope."""
        _pyproject(tmp_path, [], optional={"docs": ["mkdocs>=1.0"]})
        _uv_lock(tmp_path, requires=[])
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "mkdocs" in _text(result)

    def test_dependency_groups_are_compared(self, tmp_path):
        _pyproject(tmp_path, [], groups={"dev": ["pytest>=9.0"]})
        _uv_lock(tmp_path, requires=[], dev={"dev": [("pytest", ">=8.0")]})
        result = _run(tmp_path)
        assert result.status == "fail"
        text = _text(result)
        assert "pytest" in text and "dev" in text

    def test_a_dependency_group_that_matches_passes(self, tmp_path):
        _pyproject(tmp_path, [], groups={"dev": ["pytest>=9.0"]})
        _uv_lock(tmp_path, requires=[], dev={"dev": [("pytest", ">=9.0")]})
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_lock_without_an_entry_for_this_project_errors(self, tmp_path):
        _pyproject(tmp_path, ["requests>=2.0"])
        _uv_lock(tmp_path, requires=[("requests", ">=2.0")], source_path="other")
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "no package entry" in _text(result)

    def test_an_unreadable_lock_errors(self, tmp_path):
        _pyproject(tmp_path, ["requests>=2.0"])
        (tmp_path / "uv.lock").write_text("this is not toml [[[\n")
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "could not be read" in _text(result)

    def test_a_workspace_member_is_compared_against_the_root_lock(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
        )
        member = tmp_path / "packages" / "app"
        member.mkdir(parents=True)
        _pyproject(member, ["requests>=2.31"], name="app")
        _uv_lock(
            tmp_path, requires=[("requests", ">=2.0")], name="app",
            source_path="packages/app",
        )
        result = _run(member)
        assert result.status == "fail"
        assert "requests" in _text(result)

    def test_a_workspace_member_whose_root_lock_matches_passes(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
        )
        member = tmp_path / "packages" / "app"
        member.mkdir(parents=True)
        _pyproject(member, ["requests>=2.0"], name="app")
        _uv_lock(
            tmp_path, requires=[("requests", ">=2.0")], name="app",
            source_path="packages/app",
        )
        result = _run(member)
        assert result.status == "pass"

    def test_a_direct_reference_is_compared_by_presence(self, tmp_path):
        _pyproject(tmp_path, ["core @ file:///opt/core"])
        (tmp_path / "uv.lock").write_text(
            'version = 1\n\n[[package]]\nname = "consumer"\nversion = "0.1.0"\n'
            'source = { editable = "." }\n\n[package.metadata]\n'
            'requires-dist = [{ name = "core", directory = "/opt/core" }]\n'
        )
        result = _run(tmp_path)
        assert result.status == "pass"


class TestUvSources:
    """A ``[tool.uv.sources]`` entry erases the specifier uv records.

    Verified against uv's own output: ``libdep>=0.2`` plus
    ``libdep = { workspace = true }`` locks as
    ``{ name = "libdep", editable = "libdep" }`` -- the specifier is not
    recorded at all, because the source decides what is installed. Comparing
    the declared specifier against a lock that structurally cannot carry it
    reported every uv workspace member as a stale lock.
    """

    def test_a_workspace_sourced_dependency_is_compared_by_presence(self, tmp_path):
        _pyproject(
            tmp_path, ["libdep>=0.2"], sources={"libdep": "{ workspace = true }"},
        )
        _uv_lock(tmp_path, requires=[{"name": "libdep", "editable": "libdep"}])
        assert _run(tmp_path).status == "pass"

    def test_a_path_sourced_dependency_is_compared_by_presence(self, tmp_path):
        _pyproject(
            tmp_path, ["libdep>=0.2"],
            sources={"libdep": '{ path = "vendor/libdep" }'},
        )
        _uv_lock(
            tmp_path,
            requires=[{"name": "libdep", "directory": "vendor/libdep"}],
        )
        assert _run(tmp_path).status == "pass"

    def test_a_source_in_a_dependency_group_is_compared_by_presence(self, tmp_path):
        _pyproject(
            tmp_path, [], groups={"dev": ["libdep>=0.2"]},
            sources={"libdep": "{ workspace = true }"},
        )
        _uv_lock(
            tmp_path, requires=[],
            dev={"dev": [{"name": "libdep", "editable": "libdep"}]},
        )
        assert _run(tmp_path).status == "pass"

    def test_a_marker_gated_source_list_counts(self, tmp_path):
        """uv accepts a LIST of marker-gated source tables for one name."""
        _pyproject(
            tmp_path, ["libdep>=0.2"],
            sources={
                "libdep": '[{ workspace = true, marker = "sys_platform == \'linux\'" }]',
            },
        )
        _uv_lock(tmp_path, requires=[{"name": "libdep", "editable": "libdep"}])
        assert _run(tmp_path).status == "pass"

    def test_an_index_source_still_compares_the_specifier(self, tmp_path):
        """``index`` only picks WHERE a version comes from; the bound survives."""
        _pyproject(
            tmp_path, ["libdep>=0.2"], sources={"libdep": '{ index = "extra" }'},
        )
        _uv_lock(tmp_path, requires=[("libdep", ">=0.1")])
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "libdep" in _text(result)

    def test_a_source_added_after_the_lock_is_still_stale(self, tmp_path):
        _pyproject(
            tmp_path, ["libdep>=0.2"], sources={"libdep": "{ workspace = true }"},
        )
        _uv_lock(tmp_path, requires=[("libdep", ">=0.2")])
        assert _run(tmp_path).status == "fail"

    def test_a_source_removed_after_the_lock_is_still_stale(self, tmp_path):
        _pyproject(tmp_path, ["libdep>=0.2"])
        _uv_lock(tmp_path, requires=[{"name": "libdep", "editable": "libdep"}])
        assert _run(tmp_path).status == "fail"

    def test_a_workspace_roots_sources_reach_its_members(self, tmp_path):
        """uv applies the ROOT's [tool.uv.sources] to every member.

        A member declaring a bare sibling name is therefore source-backed even
        though its own manifest carries no sources table -- which is how every
        member of a flat uv workspace is written.
        """
        (tmp_path / "pyproject.toml").write_text(
            '[tool.uv.workspace]\nmembers = ["packages/*"]\n\n'
            '[tool.uv.sources]\nlibdep = { workspace = true }\n'
        )
        member = tmp_path / "packages" / "app"
        member.mkdir(parents=True)
        _pyproject(member, ["libdep"], name="app")
        _uv_lock(
            tmp_path, requires=[{"name": "libdep", "editable": "packages/libdep"}],
            name="app", source_path="packages/app",
        )
        assert _run(member).status == "pass"

    def test_a_member_source_overrides_the_workspace_roots(self, tmp_path):
        """The member's own entry wins, which is uv's precedence."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.uv.workspace]\nmembers = ["packages/*"]\n\n'
            '[tool.uv.sources]\nlibdep = { workspace = true }\n'
        )
        member = tmp_path / "packages" / "app"
        member.mkdir(parents=True)
        _pyproject(
            member, ["libdep>=0.2"], name="app",
            sources={"libdep": '{ index = "extra" }'},
        )
        _uv_lock(
            tmp_path, requires=[("libdep", ">=0.1")], name="app",
            source_path="packages/app",
        )
        assert _run(member).status == "fail"

    def test_a_source_backed_dependency_the_lock_never_saw_is_stale(self, tmp_path):
        _pyproject(
            tmp_path, ["libdep>=0.2"], sources={"libdep": "{ workspace = true }"},
        )
        _uv_lock(tmp_path, requires=[])
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "libdep" in _text(result)


class TestLockEntryWithoutMetadata:
    """uv omits ``[package.metadata]`` for a project that requires nothing.

    Treating the absent table as an unreadable lock made every
    dependency-free package a hard error naming a relock that cannot fix it.
    """

    def test_a_project_that_requires_nothing_passes(self, tmp_path):
        _pyproject(tmp_path, [])
        _uv_lock(tmp_path, metadata=False)
        assert _run(tmp_path).status == "pass"

    def test_a_requirement_the_lock_never_saw_is_still_reported(self, tmp_path):
        _pyproject(tmp_path, ["requests>=2.0"])
        _uv_lock(tmp_path, metadata=False)
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "requests" in _text(result)

    def test_a_malformed_metadata_table_errors(self, tmp_path):
        _pyproject(tmp_path, ["requests>=2.0"])
        (tmp_path / "uv.lock").write_text(
            'version = 1\n\n[[package]]\nname = "consumer"\nversion = "0.1.0"\n'
            'source = { editable = "." }\nmetadata = "not a table"\n'
        )
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "metadata" in _text(result)


# ---------------------------------------------------------------------------
# npm: package.json vs package-lock.json
# ---------------------------------------------------------------------------


class TestNpm:
    def test_a_lock_that_resolves_the_manifest_passes(self, tmp_path):
        _package_json(tmp_path, {"left-pad": "^1.0.0"})
        _package_lock_v3(tmp_path, {"left-pad": "^1.0.0"})
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_dependency_added_after_the_lock_errors(self, tmp_path):
        _package_json(tmp_path, {"left-pad": "^1.0.0", "chalk": "^5"})
        _package_lock_v3(tmp_path, {"left-pad": "^1.0.0"})
        result = _run(tmp_path)
        assert result.status == "fail"
        text = _text(result)
        assert "chalk" in text
        assert "npm install --package-lock-only" in text

    def test_a_changed_range_errors(self, tmp_path):
        _package_json(tmp_path, {"left-pad": "^2.0.0"})
        _package_lock_v3(tmp_path, {"left-pad": "^1.0.0"})
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "^2.0.0" in _text(result)

    def test_a_version_bump_the_lock_missed_errors(self, tmp_path):
        _package_json(tmp_path, version="0.2.0")
        _package_lock_v3(tmp_path, version="0.1.0")
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "0.2.0" in _text(result)

    def test_dev_dependencies_are_compared(self, tmp_path):
        _package_json(tmp_path, dev={"typescript": "^5"})
        _package_lock_v3(tmp_path)
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "typescript" in _text(result)

    def test_lockfile_version_1_compares_presence_only(self, tmp_path):
        _package_json(tmp_path, {"left-pad": "^1.0.0"})
        _package_lock_v1(tmp_path, {"left-pad": "1.3.0"})
        result = _run(tmp_path)
        assert result.status == "pass"
        assert "lockfileVersion 1" in result.message

    def test_lockfile_version_1_missing_dependency_errors(self, tmp_path):
        _package_json(tmp_path, {"left-pad": "^1.0.0", "chalk": "^5"})
        _package_lock_v1(tmp_path, {"left-pad": "1.3.0"})
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "chalk" in _text(result)

    def test_no_lockfile_is_a_note(self, tmp_path):
        _package_json(tmp_path, {"left-pad": "^1.0.0"})
        result = _run(tmp_path)
        assert result.status == "pass"
        assert "package-lock.json" in result.message


# ---------------------------------------------------------------------------
# go: go.mod vs go.sum
# ---------------------------------------------------------------------------


GO_MOD = """\
module example.com/consumer

go 1.23

require (
\tgithub.com/spf13/cobra v1.8.0
\tgolang.org/x/text v0.14.0 // indirect
)
"""


def _go_sum(root, entries):
    lines = []
    for module, version in entries:
        lines.append(f"{module} {version} h1:abc=")
        lines.append(f"{module} {version}/go.mod h1:def=")
    (root / "go.sum").write_text("\n".join(lines) + "\n")


class TestGo:
    def test_full_coverage_passes(self, tmp_path):
        (tmp_path / "go.mod").write_text(GO_MOD)
        _go_sum(tmp_path, [
            ("github.com/spf13/cobra", "v1.8.0"),
            ("golang.org/x/text", "v0.14.0"),
        ])
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_require_with_no_sum_errors(self, tmp_path):
        (tmp_path / "go.mod").write_text(GO_MOD)
        _go_sum(tmp_path, [("github.com/spf13/cobra", "v1.8.0")])
        result = _run(tmp_path)
        assert result.status == "fail"
        text = _text(result)
        assert "golang.org/x/text" in text
        assert "go mod tidy" in text

    def test_a_missing_go_sum_errors(self, tmp_path):
        (tmp_path / "go.mod").write_text(GO_MOD)
        result = _run(tmp_path)
        assert result.status == "fail"
        assert "no go.sum" in _text(result)

    def test_a_module_with_no_requires_owes_no_sums(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/c\n\ngo 1.23\n")
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_a_locally_replaced_module_owes_no_sum(self, tmp_path):
        (tmp_path / "go.mod").write_text(
            "module example.com/consumer\n\ngo 1.23\n\n"
            "require example.com/sibling v0.1.0\n\n"
            "replace example.com/sibling => ../sibling\n"
        )
        result = _run(tmp_path)
        assert result.status == "pass"

    def test_go_work_sum_covers_a_workspace_module(self, tmp_path):
        (tmp_path / "go.work").write_text("go 1.23\n\nuse ./mod\n")
        (tmp_path / "go.work.sum").write_text(
            "github.com/spf13/cobra v1.8.0 h1:abc=\n"
            "github.com/spf13/cobra v1.8.0/go.mod h1:def=\n"
        )
        mod = tmp_path / "mod"
        mod.mkdir()
        (mod / "go.mod").write_text(
            "module example.com/m\n\ngo 1.23\n\n"
            "require github.com/spf13/cobra v1.8.0\n"
        )
        (mod / "go.sum").write_text("")
        result = _run(mod)
        assert result.status == "pass"


class TestGoModParsing:
    def test_block_and_single_line_requires(self):
        requires, replaced = parse_go_mod(
            "module m\n\nrequire single.example/a v1.0.0\n\n"
            "require (\n\tblock.example/b v2.0.0\n)\n"
        )
        assert requires == {
            "single.example/a": "v1.0.0", "block.example/b": "v2.0.0",
        }
        assert replaced == set()

    def test_a_module_replacement_is_not_a_local_one(self):
        _requires, replaced = parse_go_mod(
            "module m\n\nreplace a.example/x => b.example/y v1.2.3\n"
        )
        assert replaced == set()

    def test_a_block_replace_of_a_local_path_is_recorded(self):
        _requires, replaced = parse_go_mod(
            "module m\n\nreplace (\n\ta.example/x => ../x\n)\n"
        )
        assert replaced == {"a.example/x"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    (">=1, <2", "<2,>=1"),
    ("<2,>=1", "<2,>=1"),
    ("", ""),
    (None, ""),
])
def test_normalize_specifier(text, expected):
    assert normalize_specifier(text) == expected


@pytest.mark.parametrize("declared,locked", [
    # Every pair below was produced by uv itself: the left spelling was
    # written into pyproject.toml, the right one is what uv.lock recorded.
    (">=1.0.0-alpha1", ">=1.0.0a1"),
    (">=1.0.0RC1", ">=1.0.0rc1"),
    (">=01.02.03", ">=1.2.3"),
    (">=1.0.0.post0,!=1.2", ">=1.0.0.post0,!=1.2"),
    ("==1.0.0+local", "==1.0.0+local"),
    ("~= 1.4", "~=1.4"),
    ("> 1 , <= 2 , != 1.5", ">1,!=1.5,<=2"),
    ("==0.19.*", "==0.19.*"),
])
def test_a_spelling_uv_canonicalizes_is_not_a_difference(declared, locked):
    assert normalize_specifier(declared) == normalize_specifier(locked)


@pytest.mark.parametrize("left,right", [
    (">=1.0.0", ">=1.0.1"),
    (">=1.0.0a1", ">=1.0.0b1"),
    (">=1.0.0a1", ">=1.0.0"),
    ("==1.0.0+local", "==1.0.0+other"),
])
def test_canonicalization_does_not_collapse_different_bounds(left, right):
    assert normalize_specifier(left) != normalize_specifier(right)


def test_arbitrary_equality_is_matched_literally(tmp_path):
    """PEP 440 says ``===`` is a raw string match, so it is not canonicalized."""
    assert normalize_specifier("===1.0.0-Alpha1") == "===1.0.0-Alpha1"


def test_evaluate_reports_every_ecosystem_it_saw(tmp_path):
    """A polyglot project's findings are not truncated to the first one."""
    _pyproject(tmp_path, ["requests>=2.0"])
    _uv_lock(tmp_path, requires=[])
    _package_json(tmp_path, {"left-pad": "^1.0.0"})
    _package_lock_v3(tmp_path)
    verdict = evaluate_dep_locks(tmp_path)
    joined = " ".join(verdict.problems)
    assert "requests" in joined and "left-pad" in joined
