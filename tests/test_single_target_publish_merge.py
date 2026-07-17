"""Single-target publish generation is unified with the multi-target path.

`run_cmd` (standalone single-target scaffold) routes its publish.yml through
the same `_generate_merged_publish` generator the multi-target path uses,
instead of the old per-pipeline raw `plan_mappings` render. Consequences:

- a lone subdir target gets `defaults.run.working-directory` injected plus
  packages-dir/version-file input rewriting (previously root-anchored);
- a root-path single-target project's publish.yml is the merged-generator
  output (job keyed by target name, one shared gate) -- the pinned canonical
  form replacing the old raw render.

The monorepo root publisher (path=".") likewise passes real per-target paths
so a subdir target renders working-directory-correct inlined jobs.
"""

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from ruamel.yaml import YAML

from rlsbl.commands.init_cmd import run_cmd, _generate_merged_publish
from rlsbl.commands.monorepo.publish_inline import generate_inline_publish_router
from rlsbl.context import ProjectContext


def _ctx(config=None):
    return ProjectContext(
        project_root=Path("."), workspace_root=None, config=config or {}
    )


def _read_publish():
    with open(os.path.join(".github", "workflows", "publish.yml")) as f:
        return f.read()


def _load(content):
    return YAML(typ="safe").load(content)


# ---------------------------------------------------------------------------
# (b) Root-path single-target: pinned canonical merged-generator output
# ---------------------------------------------------------------------------


class TestRootSingleTargetCanonical:
    """A root single-target project's publish.yml is the merged output.

    Not byte-identical to the pre-unification raw render: the job key is the
    target name (was the generic ``publish``), blank lines are normalized by
    the ruamel round-trip, and the long workflow_dispatch input description
    is folded. This is the intended, pinned canonical form.
    """

    @pytest.fixture
    def npm_root(self, mock_git_repo):
        pkg = {
            "name": "root-pkg",
            "version": "0.1.0",
            "bin": {"root-pkg": "./bin/cli.js"},
        }
        (mock_git_repo / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
        return mock_git_repo

    @pytest.fixture
    def pypi_root(self, mock_git_repo):
        (mock_git_repo / "pyproject.toml").write_text(
            '[project]\nname = "root-pkg"\nversion = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        return mock_git_repo

    def test_npm_job_keyed_by_target_not_publish(self, npm_root):
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {}, ctx=_ctx())
        content = _read_publish()
        # New canonical: job key is the target name; old raw render used
        # the generic "publish" job key.
        assert "\n  npm:" in content
        assert "\n  publish:" not in content
        # Exactly one shared gate covering the target's CI.
        assert "\n  gate:" in content
        # Root target: no working-directory injected.
        assert "working-directory" not in content
        # Vars resolved, no leftover template placeholders.
        assert "{{" not in content.replace("${{", "")

    def test_pypi_job_keyed_by_target_not_publish(self, pypi_root):
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("pypi", [], {}, ctx=_ctx())
        content = _read_publish()
        assert "\n  pypi:" in content
        assert "\n  publish:" not in content
        assert "\n  gate:" in content
        assert "working-directory" not in content

    def test_output_equals_merged_generator(self, npm_root):
        """run_cmd's publish.yml is exactly _generate_merged_publish's output.

        Proves the single-target path routes through the merged generator
        (pinned canonical form) rather than a divergent raw render.
        """
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {}, ctx=_ctx())
        actual = _read_publish()

        # Rebuild the vars the same way run_cmd does and regenerate.
        from datetime import datetime
        from rlsbl.targets import TARGETS
        from rlsbl.pipelines import load_pipelines
        from rlsbl.commands.init_cmd import _npm_provenance_var
        from rlsbl.publish_gate import (
            ci_check_regex_for_targets,
            gate_job_template_snippet,
        )

        cfg = json.loads((Path(".rlsbl") / "config.json").read_text())
        ctx = _ctx(cfg)
        reg = TARGETS["npm"]
        vars_dict = reg.template_vars(".", ctx)
        vars_dict["year"] = str(datetime.now().year)
        vars_dict["npm.provenance"] = _npm_provenance_var(cfg)
        vars_dict["publishGate"] = gate_job_template_snippet(
            ci_check_regex_for_targets(["npm"])
        )
        expected = _generate_merged_publish(
            ["npm"], vars_dict, {"npm": "."},
            pipelines=load_pipelines(cfg),
        )
        assert actual == expected


# ---------------------------------------------------------------------------
# (a) Lone subdir standalone target: working-directory + rewritten inputs
# ---------------------------------------------------------------------------


class TestLoneSubdirStandalone:
    """A single target declared in a subdirectory gets a subdir-aware
    publish.yml, not a root-anchored one."""

    @pytest.fixture
    def npm_subdir(self, mock_git_repo):
        sub = mock_git_repo / "npmpkg"
        sub.mkdir()
        pkg = {
            "name": "sub-pkg",
            "version": "0.1.0",
            "bin": {"sub-pkg": "./bin/cli.js"},
        }
        (sub / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
        cfg = {
            "targets": [{"name": "npm", "path": "npmpkg"}],
            "publish_mode": "ci",
        }
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        return mock_git_repo, cfg

    @pytest.fixture
    def pypi_subdir(self, mock_git_repo):
        sub = mock_git_repo / "py"
        sub.mkdir()
        (sub / "pyproject.toml").write_text(
            '[project]\nname = "sub-pkg"\nversion = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        cfg = {
            "targets": [{"name": "pypi", "path": "py"}],
            "publish_mode": "ci",
        }
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        return mock_git_repo, cfg

    def test_npm_subdir_gets_working_directory(self, npm_subdir):
        _, cfg = npm_subdir
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("npm", [], {}, ctx=_ctx(cfg))
        data = _load(_read_publish())
        npm_job = data["jobs"]["npm"]
        assert npm_job["defaults"]["run"]["working-directory"] == "./npmpkg"
        # npm-specific content still resolves (registry-url is a value, so the
        # publish job stays wired correctly).
        content = _read_publish()
        assert "registry.npmjs.org" in content
        assert "{{" not in content.replace("${{", "")

    def test_pypi_subdir_gets_working_directory_and_packages_dir(self, pypi_subdir):
        _, cfg = pypi_subdir
        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("pypi", [], {}, ctx=_ctx(cfg))
        data = _load(_read_publish())
        pypi_job = data["jobs"]["pypi"]
        assert pypi_job["defaults"]["run"]["working-directory"] == "./py"
        # The pypi publish action's packages-dir is prefixed with the subdir
        # (action inputs are repo-root-relative, unaffected by working-directory).
        publish_step = next(
            s for s in pypi_job["steps"]
            if "pypa/gh-action-pypi-publish" in s.get("uses", "")
        )
        assert publish_step["with"]["packages-dir"] == "./py/dist/"


# ---------------------------------------------------------------------------
# (c) Monorepo root publisher (path=".") with a subdir target
# ---------------------------------------------------------------------------


def _setup_root_publisher_subdir(root, *, name, sub, gate_regex):
    """Root (path='.') publisher whose npm target lives in subdir *sub*."""
    subdir = os.path.join(root, sub)
    os.makedirs(subdir, exist_ok=True)
    pkg = {"name": name, "version": "1.2.3", "bin": {name: "./bin/cli.js"}}
    with open(os.path.join(subdir, "package.json"), "w") as f:
        json.dump(pkg, f)

    mono = os.path.join(root, ".rlsbl-monorepo")
    rel_dir = os.path.join(mono, "releasables", name)
    os.makedirs(rel_dir, exist_ok=True)
    cfg = {
        "publish_mode": "ci",
        "targets": [{"name": "npm", "path": sub}],
        "publish_gate_check_regex": gate_regex,
    }
    with open(os.path.join(rel_dir, "config.json"), "w") as f:
        json.dump(cfg, f)
    with open(os.path.join(mono, "workspace.toml"), "w") as f:
        f.write(
            f'[[releasables]]\nname = "{name}"\n\n'
            f'[[projects]]\npath = "."\nreleasable = "{name}"\n'
        )
    return {"name": name, "path": ".", "releasable": name, "_root_publisher": True}


class TestRootPublisherSubdirTarget:
    def test_subdir_target_working_directory_correct(self, tmp_path):
        root = str(tmp_path)
        regex = r"^(test)( \(.*\))?$"
        proj = _setup_root_publisher_subdir(
            root, name="orxtra", sub="npm", gate_regex=regex
        )
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="orxtra@v",
        ):
            result = generate_inline_publish_router([proj], root)

        data = _load(result)
        jobs = data["jobs"]
        assert "gate" in jobs
        pub_keys = [k for k in jobs if k.startswith("orxtra-")]
        assert pub_keys, f"no root publish jobs: {list(jobs)}"
        # The publish job runs in the target's subdirectory. project_path is "."
        # (root publisher), composed with the target subdir "npm" -> "npm".
        for k in pub_keys:
            wd = jobs[k]["defaults"]["run"]["working-directory"]
            assert wd == "npm", f"{k} working-directory={wd!r}, expected 'npm'"

    def test_root_target_still_no_subdir(self, tmp_path):
        """A root publisher whose target is at repo root keeps working-directory
        '.' -- the subdir composition must not disturb the common case."""
        root = str(tmp_path)
        name = "orxtra"
        with open(os.path.join(root, "pyproject.toml"), "w") as f:
            f.write(f'[project]\nname = "{name}"\nversion = "1.2.3"\n')
        mono = os.path.join(root, ".rlsbl-monorepo")
        rel_dir = os.path.join(mono, "releasables", name)
        os.makedirs(rel_dir, exist_ok=True)
        with open(os.path.join(rel_dir, "config.json"), "w") as f:
            json.dump(
                {
                    "publish_mode": "ci",
                    "targets": ["pypi"],
                    "publish_gate_check_regex": r"^(test)( \(.*\))?$",
                },
                f,
            )
        with open(os.path.join(mono, "workspace.toml"), "w") as f:
            f.write(
                f'[[releasables]]\nname = "{name}"\n\n'
                f'[[projects]]\npath = "."\nreleasable = "{name}"\n'
            )
        proj = {"name": name, "path": ".", "releasable": name, "_root_publisher": True}
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="orxtra@v",
        ):
            result = generate_inline_publish_router([proj], root)
        data = _load(result)
        for k in [k for k in data["jobs"] if k.startswith("orxtra-")]:
            assert data["jobs"][k]["defaults"]["run"]["working-directory"] == "."
