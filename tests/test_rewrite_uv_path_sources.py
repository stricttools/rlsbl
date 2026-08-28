"""``rlsbl rewrite uv-path-sources``: the conversion, the probe, the counts.

A path-sourced dependency resolves from a checkout that only exists on the
developer's machine. Converting it means writing a registry constraint floored
at the version the lock already resolves -- and the whole value of the command
is that the floor it writes is one a consumer can actually satisfy. So the
refusals are as important as the rewrite, and they are pinned here:

* a locked version PyPI does not have is a hard error naming the remedy;
* a probe that fails to answer is a hard error too (fail-closed: silence is
  not evidence of publication);
* a package the lock does not resolve is a hard error;
* a count that moved between preview and apply aborts with nothing written.

The other half pinned here is WHERE the lock is read from. A uv workspace
member has no lock of its own -- one lock at the workspace root resolves the
whole workspace -- so the lock location is resolved by walking up to the
workspace root that claims the target directory. A lock beside the manifest
always wins, and neither location having a lock keeps the hard error.
"""

import json
import textwrap

import pytest
import tomlkit

from rlsbl.commands.rewrite.uv_path_sources import (
    CONFIG_ITEM_KEY,
    UvPathSourceError,
    apply_item,
    cmd_uv_path_sources,
    collect_conversions,
    count_entries,
    observe,
    path_sourced_names,
)
from rlsbl.dep_rewrite import (
    SECTIONS_ALL,
    detect_uv_path_sources,
    find_dep_entries,
    floor_dep_entries,
    remove_uv_sources,
)


def _published(name, version):
    return {"status": "found"}


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _project(tmp_path, manifest, lock, config="{}\n"):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".rlsbl").mkdir()
    _write(root, ".rlsbl/config.json", config)
    _write(root, "pyproject.toml", textwrap.dedent(manifest))
    _write(root, "uv.lock", textwrap.dedent(lock))
    return root


LOCK = """\
    version = 1

    [[package]]
    name = "core"
    version = "1.2.3"

    [[package]]
    name = "helper"
    version = "0.9.0"

    [[package]]
    name = "requests"
    version = "2.31.0"
"""


# ---------------------------------------------------------------------------
# The dep_rewrite primitives both consumers share
# ---------------------------------------------------------------------------


class TestSharedPrimitives:
    def test_dependency_groups_are_visible_to_the_all_family(self):
        doc = tomlkit.parse(textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["core"]

            [project.optional-dependencies]
            cli = ["core[rich]"]

            [dependency-groups]
            dev = ["helper", "pytest"]
        """))
        found = find_dep_entries(
            doc, {"core": "core", "helper": "helper"}, SECTIONS_ALL,
        )
        assert sorted(e["section"] for e in found) == [
            "dependencies", "dependency-groups.dev", "optional-dependencies.cli",
        ]

    def test_flooring_preserves_extras_and_markers(self):
        doc = tomlkit.parse(textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = [
                "core[rich]>=0.1 ; python_version < '3.13'",
                "core @ file:///checkout/core",
                "requests>=2.0",
            ]
        """))
        changed = floor_dep_entries(doc, {"core": "1.2.3"}, SECTIONS_ALL)
        assert changed == 2
        deps = [str(d) for d in doc["project"]["dependencies"]]
        # The marker is carried verbatim from the ';' onward; the whitespace
        # that preceded the ';' is not part of it and is not reproduced.
        assert deps[0] == "core[rich]>=1.2.3; python_version < '3.13'"
        assert deps[1] == "core>=1.2.3"
        assert deps[2] == "requests>=2.0"

    def test_removing_the_last_source_removes_the_table(self):
        doc = tomlkit.parse(textwrap.dedent("""\
            [project]
            name = "app"

            [tool.uv.sources]
            core = { path = "../core" }
        """))
        assert remove_uv_sources(doc, ["core"]) == 1
        assert "tool" not in tomlkit.dumps(doc)

    def test_a_sibling_source_survives_its_neighbour_being_removed(self):
        doc = tomlkit.parse(textwrap.dedent("""\
            [tool.uv.sources]
            core = { path = "../core" }
            helper = { workspace = true }
        """))
        assert remove_uv_sources(doc, ["core"]) == 1
        assert detect_uv_path_sources(doc) == {"helper": "workspace"}

    def test_a_mixed_list_keeps_its_non_path_element(self):
        """A marker-gated list loses only its path elements.

        uv accepts a LIST of marker-gated tables for one package. Deleting the
        whole key because ONE element is a path source silently drops the
        others -- here, the index the package resolves from everywhere else.
        """
        doc = tomlkit.parse(textwrap.dedent("""\
            [tool.uv.sources]
            core = [
                { path = "../core", marker = "sys_platform == 'linux'" },
                { index = "testpypi", marker = "sys_platform != 'linux'" },
            ]
        """))
        assert remove_uv_sources(doc, ["core"]) == 1
        assert detect_uv_path_sources(doc) == {}
        remaining = doc["tool"]["uv"]["sources"]["core"]
        assert [dict(e) for e in remaining] == [
            {"index": "testpypi", "marker": "sys_platform != 'linux'"},
        ]

    def test_a_list_of_only_path_elements_loses_the_whole_key(self):
        doc = tomlkit.parse(textwrap.dedent("""\
            [tool.uv.sources]
            core = [
                { path = "../core", marker = "sys_platform == 'linux'" },
                { workspace = true, marker = "sys_platform != 'linux'" },
            ]
        """))
        assert remove_uv_sources(doc, ["core"]) == 1
        assert "tool" not in tomlkit.dumps(doc)

    def test_an_array_of_tables_keeps_its_non_path_element(self):
        """The same source declared as [[tool.uv.sources.core]] tables."""
        doc = tomlkit.parse(textwrap.dedent("""\
            [[tool.uv.sources.core]]
            path = "../core"
            marker = "sys_platform == 'linux'"

            [[tool.uv.sources.core]]
            index = "testpypi"
            marker = "sys_platform != 'linux'"
        """))
        assert remove_uv_sources(doc, ["core"]) == 1
        assert detect_uv_path_sources(doc) == {}
        assert "index" in tomlkit.dumps(doc)
        assert "../core" not in tomlkit.dumps(doc)

    def test_git_and_url_sources_are_not_path_sources(self):
        doc = tomlkit.parse(textwrap.dedent("""\
            [tool.uv.sources]
            core = { git = "https://example.invalid/core" }
            helper = { path = "../helper" }
        """))
        assert detect_uv_path_sources(doc) == {"helper": "path"}


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


class TestObserve:
    def test_both_declaration_shapes_are_found(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = [
                "core",
                "helper @ file:///checkout/helper",
                "requests>=2.0",
            ]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK)
        doc = tomlkit.parse((root / "pyproject.toml").read_text())
        assert path_sourced_names(doc) == {"core": "core", "helper": "helper"}

    def test_a_conversion_counts_its_entries(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]

            [project.optional-dependencies]
            cli = ["core"]

            [dependency-groups]
            dev = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK)
        doc = tomlkit.parse((root / "pyproject.toml").read_text())
        assert count_entries(doc, "core") == (3, 1)

        from rlsbl.dep_floors import pypi_locked
        conv = collect_conversions(doc, pypi_locked(str(root)))[0]
        assert conv.occurrences == 4
        assert conv.locked_version == "1.2.3"
        assert conv.constraint == ">=1.2.3"

    def test_the_preview_ends_with_the_config_item(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK)
        preview = observe(root, probe=lambda n, v: None)
        assert preview.keys == ("core", CONFIG_ITEM_KEY)
        config_item = preview.by_key(CONFIG_ITEM_KEY)
        assert config_item.state == "declare_floors"
        assert "core" in config_item.summary

    def test_an_already_declared_floor_needs_no_config_write(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK, config=json.dumps({"internal_dep_floors": ["core"]}))
        preview = observe(root, probe=lambda n, v: None)
        item = preview.by_key(CONFIG_ITEM_KEY)
        assert item.state == "floors_already_declared"
        assert item.actions == ()

    def test_nothing_to_convert(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["requests>=2.0"]
        """, LOCK)
        preview = observe(root, probe=lambda n, v: None)
        assert preview.only().state == "nothing_to_convert"

    def test_a_package_the_lock_does_not_resolve_refuses(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["ghost"]

            [tool.uv.sources]
            ghost = { path = "../ghost" }
        """, LOCK)
        with pytest.raises(UvPathSourceError, match="uv.lock does not resolve"):
            observe(root, probe=lambda n, v: None)

    def test_a_missing_lock_refuses(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]
        """, LOCK)
        (root / "uv.lock").unlink()
        with pytest.raises(UvPathSourceError, match="no uv.lock"):
            observe(root, probe=lambda n, v: None)

    def test_a_missing_manifest_refuses(self, tmp_path):
        root = _project(tmp_path, "[project]\nname = 'app'\n", LOCK)
        (root / "pyproject.toml").unlink()
        with pytest.raises(UvPathSourceError, match="no pyproject.toml"):
            observe(root, probe=lambda n, v: None)


# ---------------------------------------------------------------------------
# The release-first probe
# ---------------------------------------------------------------------------


class TestTheProbe:
    def _root(self, tmp_path):
        return _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK)

    def test_unpublished_names_the_release_first_remedy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "rlsbl.commands.rewrite.uv_path_sources.query_pypi_release",
            lambda name, version: {"status": "not_found"},
        )
        with pytest.raises(UvPathSourceError) as exc:
            observe(self._root(tmp_path))
        assert "Release core first" in str(exc.value)
        assert "1.2.3" in str(exc.value)

    def test_a_failed_probe_refuses_rather_than_assuming(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "rlsbl.commands.rewrite.uv_path_sources.query_pypi_release",
            lambda name, version: {"status": "error", "message": "HTTP 503"},
        )
        with pytest.raises(UvPathSourceError) as exc:
            observe(self._root(tmp_path))
        assert "HTTP 503" in str(exc.value)
        assert "unanswered probe" in str(exc.value)

    def test_the_exact_locked_version_is_probed(self, tmp_path, monkeypatch):
        asked = []
        monkeypatch.setattr(
            "rlsbl.commands.rewrite.uv_path_sources.query_pypi_release",
            lambda name, version: (asked.append((name, version)),
                                   {"status": "found"})[1],
        )
        observe(self._root(tmp_path))
        assert asked == [("core", "1.2.3")]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class TestApply:
    def test_the_full_conversion(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = [
                "core",
                "requests>=2.0",
            ]

            [dependency-groups]
            dev = ["helper @ file:///checkout/helper"]

            [tool.uv.sources]
            core = { path = "../core", editable = true }
            helper = { workspace = true }
        """, LOCK)
        for item in observe(root, probe=_published).items:
            apply_item(item, root)

        manifest = (root / "pyproject.toml").read_text()
        doc = tomlkit.parse(manifest)
        assert [str(d) for d in doc["project"]["dependencies"]] == [
            "core>=1.2.3", "requests>=2.0",
        ]
        assert [str(d) for d in doc["dependency-groups"]["dev"]] == [
            "helper>=0.9.0",
        ]
        assert "tool" not in doc

        config = json.loads((root / ".rlsbl" / "config.json").read_text())
        assert config["internal_dep_floors"] == ["core", "helper"]

    def test_the_config_key_is_created_when_absent(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK, config=json.dumps({"publish_mode": "ci"}))
        for item in observe(root, probe=_published).items:
            apply_item(item, root)
        config = json.loads((root / ".rlsbl" / "config.json").read_text())
        assert config["internal_dep_floors"] == ["core"]
        assert config["publish_mode"] == "ci"

    def test_an_existing_floor_list_is_extended_not_replaced(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK, config=json.dumps({"internal_dep_floors": ["stricttest"]}))
        for item in observe(root, probe=_published).items:
            apply_item(item, root)
        config = json.loads((root / ".rlsbl" / "config.json").read_text())
        assert config["internal_dep_floors"] == ["core", "stricttest"]

    def test_a_mixed_source_list_survives_the_conversion(self, tmp_path):
        """End to end: the floor is written and the index sibling stays."""
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core", "requests>=2.0"]

            [tool.uv.sources]
            core = [
                { path = "../core", marker = "sys_platform == 'linux'" },
                { index = "testpypi", marker = "sys_platform != 'linux'" },
            ]
        """, LOCK)
        for item in observe(root, probe=_published).items:
            apply_item(item, root)

        doc = tomlkit.parse((root / "pyproject.toml").read_text())
        assert [str(d) for d in doc["project"]["dependencies"]] == [
            "core>=1.2.3", "requests>=2.0",
        ]
        assert [dict(e) for e in doc["tool"]["uv"]["sources"]["core"]] == [
            {"index": "testpypi", "marker": "sys_platform != 'linux'"},
        ]

    def test_comments_and_unrelated_content_survive(self, tmp_path):
        root = _project(tmp_path, """\
            # top of file
            [project]
            name = "app"
            dependencies = [
                "core",  # the sibling
                "requests>=2.0",
            ]

            [tool.ruff]
            line-length = 100

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK)
        for item in observe(root, probe=_published).items:
            apply_item(item, root)
        manifest = (root / "pyproject.toml").read_text()
        assert "# top of file" in manifest
        assert "# the sibling" in manifest
        assert "[tool.ruff]" in manifest
        assert "line-length = 100" in manifest
        assert "[tool.uv.sources]" not in manifest


class TestTheCountContract:
    def test_a_count_that_moved_aborts_without_writing(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK)
        preview = observe(root, probe=_published)
        item = preview.by_key("core")

        # The manifest moves underneath the plan: core gains an extra entry.
        _write(root, "pyproject.toml", textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["core"]

            [dependency-groups]
            dev = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """))
        before = (root / "pyproject.toml").read_text()

        with pytest.raises(UvPathSourceError) as exc:
            apply_item(item, root)
        assert "counted 2" in str(exc.value)
        assert "nothing further has been written" in str(exc.value)
        assert (root / "pyproject.toml").read_text() == before

    def test_the_abort_names_the_dependencies_already_converted(self, tmp_path):
        """An abort is not a rollback: the earlier writes are on disk."""
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core", "helper"]

            [tool.uv.sources]
            core = { path = "../core" }
            helper = { path = "../helper" }
        """, LOCK)
        preview = observe(root, probe=_published)
        applied = []
        apply_item(preview.by_key("core"), root, applied=applied)

        # helper gains an entry underneath the plan.
        doc = tomlkit.parse((root / "pyproject.toml").read_text())
        doc["project"]["dependencies"].append("helper")
        _write(root, "pyproject.toml", tomlkit.dumps(doc))

        with pytest.raises(UvPathSourceError) as exc:
            apply_item(preview.by_key("helper"), root, applied=applied)
        message = str(exc.value)
        assert "Already written by this run" in message
        assert "core" in message
        assert "re-plans from the manifest as it is now" in message

    def test_the_abort_says_so_when_nothing_was_written_yet(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK)
        preview = observe(root, probe=_published)
        _write(root, "pyproject.toml", textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = ["core"]

            [dependency-groups]
            dev = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """))
        with pytest.raises(UvPathSourceError) as exc:
            apply_item(preview.by_key("core"), root, applied=[])
        assert "Nothing had been written" in str(exc.value)

    def test_a_manifest_that_vanished_before_apply_is_a_clean_error(self, tmp_path):
        root = _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK)
        preview = observe(root, probe=_published)
        (root / "pyproject.toml").unlink()
        with pytest.raises(UvPathSourceError) as exc:
            apply_item(preview.by_key("core"), root, applied=[])
        assert "no pyproject.toml" in str(exc.value)


class TestCommandEntryPoint:
    def _root(self, tmp_path):
        return _project(tmp_path, """\
            [project]
            name = "app"
            dependencies = ["core"]

            [tool.uv.sources]
            core = { path = "../core" }
        """, LOCK)

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.rewrite.uv_path_sources.query_pypi_release",
            _published,
        )
        root = self._root(tmp_path)
        before = (root / "pyproject.toml").read_text()
        cmd_uv_path_sources({"dry-run": True}, project_root=root)
        out = capsys.readouterr().out
        assert "core: convert" in out
        assert "core>=1.2.3" in out
        assert (root / "pyproject.toml").read_text() == before
        assert "internal_dep_floors" not in (
            root / ".rlsbl" / "config.json"
        ).read_text()

    def test_a_refusal_exits_one(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.rewrite.uv_path_sources.query_pypi_release",
            lambda name, version: {"status": "not_found"},
        )
        root = self._root(tmp_path)
        with pytest.raises(SystemExit) as exc:
            cmd_uv_path_sources({"dry-run": True}, project_root=root)
        assert exc.value.code == 1
        assert "Release core first" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Where the lock is read from
# ---------------------------------------------------------------------------


MEMBER_MANIFEST = """\
    [project]
    name = "app"
    dependencies = ["core", "requests>=2.0"]

    [tool.uv.sources]
    core = { path = "../core" }
"""

#: A root lock resolving a DIFFERENT version, so precedence is observable.
ROOT_LOCK_AHEAD = """\
    version = 1

    [[package]]
    name = "core"
    version = "9.9.9"
"""


def _workspace(
    tmp_path,
    *,
    members,
    exclude=None,
    root_lock=LOCK,
    member_lock=None,
    member_rel="packages/app",
    manifest=MEMBER_MANIFEST,
    config=None,
):
    """A uv workspace: root manifest + root lock, one member, no member lock."""
    ws = tmp_path / "ws"
    ws.mkdir()
    table = "[tool.uv.workspace]\nmembers = " + json.dumps(members) + "\n"
    if exclude is not None:
        table += "exclude = " + json.dumps(exclude) + "\n"
    _write(ws, "pyproject.toml", table)
    if root_lock is not None:
        _write(ws, "uv.lock", textwrap.dedent(root_lock))
    member = ws / member_rel
    _write(member, "pyproject.toml", textwrap.dedent(manifest))
    _write(ws, "packages/core/pyproject.toml", '[project]\nname = "core"\n')
    if member_lock is not None:
        _write(member, "uv.lock", textwrap.dedent(member_lock))
    if config is not None:
        _write(member, ".rlsbl/config.json", config)
    return ws, member


class TestLockLocation:
    def test_a_workspace_member_reads_the_root_lock(self, tmp_path):
        """The member has no lock of its own; the workspace root's lock rules."""
        ws, member = _workspace(tmp_path, members=["packages/*"])
        preview = observe(member, probe=_published)
        assert preview.keys == ("core", CONFIG_ITEM_KEY)
        for item in preview.items:
            apply_item(item, member)

        doc = tomlkit.parse((member / "pyproject.toml").read_text())
        assert [str(d) for d in doc["project"]["dependencies"]] == [
            "core>=1.2.3", "requests>=2.0",
        ]
        assert "tool" not in doc
        # The rewrite stays inside the member: the workspace root is only read.
        assert "[tool.uv.workspace]" in (ws / "pyproject.toml").read_text()

    def test_the_member_config_is_created_with_the_floors_key(self, tmp_path):
        ws, member = _workspace(tmp_path, members=["packages/*"])
        assert not (member / ".rlsbl" / "config.json").exists()
        for item in observe(member, probe=_published).items:
            apply_item(item, member)
        config = json.loads((member / ".rlsbl" / "config.json").read_text())
        assert config == {"internal_dep_floors": ["core"]}

    def test_the_plan_names_the_lock_it_read(self, tmp_path):
        ws, member = _workspace(tmp_path, members=["packages/*"])
        item = observe(member, probe=_published).by_key("core")
        facts = "\n".join(item.facts)
        assert "uv.lock" in facts
        assert "workspace root" in facts

    def test_a_lock_beside_the_manifest_wins(self, tmp_path):
        """Both locks exist and disagree: the one beside the manifest decides."""
        ws, member = _workspace(
            tmp_path, members=["packages/*"], root_lock=ROOT_LOCK_AHEAD,
            member_lock=LOCK,
        )
        for item in observe(member, probe=_published).items:
            apply_item(item, member)
        doc = tomlkit.parse((member / "pyproject.toml").read_text())
        assert [str(d) for d in doc["project"]["dependencies"]] == [
            "core>=1.2.3", "requests>=2.0",
        ]

    def test_a_literal_member_path_matches(self, tmp_path):
        ws, member = _workspace(tmp_path, members=["packages/app"])
        assert observe(member, probe=_published).by_key("core") is not None

    def test_a_recursive_glob_matches(self, tmp_path):
        ws, member = _workspace(
            tmp_path, members=["packages/**"], member_rel="packages/py/app",
        )
        assert observe(member, probe=_published).by_key("core") is not None

    def test_a_member_glob_does_not_cross_a_directory_separator(self, tmp_path):
        """``packages/*`` claims ``packages/app``, never ``packages/py/app``."""
        ws, member = _workspace(
            tmp_path, members=["packages/*"], member_rel="packages/py/app",
        )
        with pytest.raises(UvPathSourceError, match="no uv.lock"):
            observe(member, probe=_published)

    def test_an_excluded_directory_does_not_reach_the_root_lock(self, tmp_path):
        ws, member = _workspace(
            tmp_path, members=["packages/*"], exclude=["packages/app"],
        )
        with pytest.raises(UvPathSourceError, match="no uv.lock") as exc:
            observe(member, probe=_published)
        assert "workspace" in str(exc.value)

    def test_a_directory_no_member_glob_claims_does_not_reach_the_root_lock(
        self, tmp_path,
    ):
        ws, member = _workspace(tmp_path, members=["other/*"])
        with pytest.raises(UvPathSourceError, match="no uv.lock"):
            observe(member, probe=_published)

    def test_the_both_absent_error_names_both_locations(self, tmp_path):
        ws, member = _workspace(
            tmp_path, members=["packages/*"], root_lock=None,
        )
        with pytest.raises(UvPathSourceError) as exc:
            observe(member, probe=_published)
        message = str(exc.value)
        assert str(member / "uv.lock") in message
        assert str(ws / "uv.lock") in message

    def test_no_workspace_above_names_the_one_location_probed(self, tmp_path):
        root = _project(tmp_path, MEMBER_MANIFEST, LOCK)
        (root / "uv.lock").unlink()
        with pytest.raises(UvPathSourceError) as exc:
            observe(root, probe=_published)
        message = str(exc.value)
        assert str(root / "uv.lock") in message
        assert "workspace" in message

    def test_an_unreadable_lock_is_not_treated_as_an_absent_one(self, tmp_path):
        """A broken lock beside the manifest refuses -- it never walks past it."""
        ws, member = _workspace(
            tmp_path, members=["packages/*"], root_lock=ROOT_LOCK_AHEAD,
            member_lock="this is not toml [[[\n",
        )
        with pytest.raises(UvPathSourceError, match="could not be read as TOML"):
            observe(member, probe=_published)

    def test_an_ancestor_without_a_workspace_table_is_not_a_root(self, tmp_path):
        """A parent that merely has a pyproject.toml and a lock is not a root."""
        ws, member = _workspace(tmp_path, members=["packages/*"])
        _write(ws, "pyproject.toml", '[project]\nname = "ws"\n')
        with pytest.raises(UvPathSourceError, match="no uv.lock"):
            observe(member, probe=_published)
