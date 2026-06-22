"""Verification tests for the multi_releasable_monorepo fixture.

Ensures the fixture builds a valid monorepo with releasable structure
that load_releasables() and members_of() can consume correctly.
"""

import json
import os

from rlsbl.workspace import (
    load_releasables,
    load_workspace,
    members_of,
    get_releasable_changes_dir,
    get_releasable_dir,
    read_releasable_version,
)


class TestMultiReleasableFixtureDefaults:
    """Tests using the default multi_releasable_monorepo fixture."""

    def test_load_releasables_returns_two(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        releasables = load_releasables(str(ns.root))
        assert len(releasables) == 2
        names = {r.name for r in releasables}
        assert names == {"alpha", "beta"}

    def test_alpha_has_two_members(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        projects = load_workspace(str(ns.root))
        members = members_of("alpha", projects)
        assert len(members) == 2
        member_names = {m.name for m in members}
        assert member_names == {"alpha-core", "alpha-web"}

    def test_beta_has_two_members(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        projects = load_workspace(str(ns.root))
        members = members_of("beta", projects)
        assert len(members) == 2
        member_names = {m.name for m in members}
        assert member_names == {"beta-api", "beta-cli"}

    def test_dev_only_project_not_in_any_releasable(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        projects = load_workspace(str(ns.root))
        devtools = [p for p in projects if p.name == "devtools"]
        assert len(devtools) == 1
        assert devtools[0].dev_only is True
        # Should not appear as a member of either releasable
        for rel_name in ("alpha", "beta"):
            members = members_of(rel_name, projects)
            assert all(m.name != "devtools" for m in members)

    def test_releasable_version_files_exist(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        for rel in ns.releasables:
            version = read_releasable_version(str(ns.root), rel.name)
            assert version == ns.initial_version

    def test_releasable_changes_dirs_exist(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        for rel in ns.releasables:
            changes_dir = get_releasable_changes_dir(str(ns.root), rel.name)
            assert os.path.isdir(changes_dir)
            unreleased = os.path.join(changes_dir, "unreleased.jsonl")
            assert os.path.isfile(unreleased)

    def test_releasable_config_json_exists(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        for rel in ns.releasables:
            rel_dir = get_releasable_dir(str(ns.root), rel.name)
            config_path = os.path.join(rel_dir, "config.json")
            assert os.path.isfile(config_path)
            with open(config_path) as f:
                config = json.load(f)
            assert isinstance(config, dict)

    def test_project_directories_created(self, multi_releasable_monorepo):
        ns = multi_releasable_monorepo
        for proj_name, proj_dir in ns.project_dirs.items():
            assert proj_dir.is_dir(), f"{proj_name} dir missing"
            assert (proj_dir / "pyproject.toml").is_file()

    def test_git_tags_exist(self, multi_releasable_monorepo):
        """Each releasable should have a version tag."""
        import subprocess

        ns = multi_releasable_monorepo
        result = subprocess.run(
            ["git", "tag", "--list"],
            cwd=str(ns.root),
            capture_output=True,
            text=True,
            check=True,
        )
        tags = set(result.stdout.strip().split("\n"))
        for rel in ns.releasables:
            expected_tag = rel.tag_format.format(
                name=rel.name, version=ns.initial_version
            )
            assert expected_tag in tags, f"tag {expected_tag} not found in {tags}"


class TestMultiReleasableFactory:
    """Tests using the factory fixture for custom configurations."""

    def test_custom_releasable_config(self, multi_releasable_monorepo_factory):
        ns = multi_releasable_monorepo_factory(
            releasable_configs={
                "alpha": {"batch_limits": {"max_commits_per_entry": 3}},
            },
        )
        rel_dir = get_releasable_dir(str(ns.root), "alpha")
        config_path = os.path.join(rel_dir, "config.json")
        with open(config_path) as f:
            config = json.load(f)
        assert config["batch_limits"]["max_commits_per_entry"] == 3

    def test_custom_publish_config(self, multi_releasable_monorepo_factory):
        publish = {"pipelines": [{"type": "pypi", "local": False}]}
        ns = multi_releasable_monorepo_factory(
            publish_configs={"beta": publish},
        )
        rel_dir = get_releasable_dir(str(ns.root), "beta")
        publish_path = os.path.join(rel_dir, "publish.json")
        assert os.path.isfile(publish_path)
        with open(publish_path) as f:
            data = json.load(f)
        assert data["pipelines"][0]["type"] == "pypi"

    def test_custom_hook_config(self, multi_releasable_monorepo_factory):
        ns = multi_releasable_monorepo_factory(
            hook_configs={
                "alpha": {
                    "pre-checks.sh": "#!/bin/bash\necho pre-checks\n",
                    "pre-release.sh": "#!/bin/bash\necho pre-release\n",
                },
            },
        )
        rel_dir = get_releasable_dir(str(ns.root), "alpha")
        hook_path = os.path.join(rel_dir, "hooks", "pre-checks.sh")
        assert os.path.isfile(hook_path)
        assert os.access(hook_path, os.X_OK)

    def test_custom_initial_version(self, multi_releasable_monorepo_factory):
        ns = multi_releasable_monorepo_factory(initial_version="1.0.0")
        for rel in ns.releasables:
            version = read_releasable_version(str(ns.root), rel.name)
            assert version == "1.0.0"
