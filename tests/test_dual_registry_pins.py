"""Dual-registry layout pins (Phase 6.1).

These tests pin the exact scan-depth and layout contract for members that
publish to more than one registry:

- Target auto-detection is DEPTH-ZERO: ``_auto_detect`` inspects only the
  single directory it is handed. Co-located manifests (``pyproject.toml`` +
  ``package.json`` in the same dir) yield BOTH targets; a manifest that lives
  in a subdirectory (``npm/package.json``) is invisible to auto-detection and
  is reachable ONLY via a config-declared ``{"name": ..., "path": ...}`` entry.
- The inlined publish router carries every one of a dual-target member's
  publish jobs, wires them all to the single shared gate, and gates them all
  behind the same ref-based tag condition.
"""

import os

from ruamel.yaml import YAML

from rlsbl.targets import (
    TargetEntry,
    _auto_detect,
    _parse_target_entry,
    detect_targets,
)
from rlsbl.commands.init_cmd import _generate_merged_publish
from rlsbl.commands.monorepo.publish_inline import generate_inline_publish_router
from rlsbl.publish_gate import GATE_JOB_KEY


def _write_pyproject(dir_path, name="dual"):
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "pyproject.toml"), "w", encoding="utf-8") as f:
        f.write(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')


def _write_package_json(dir_path, name="dual"):
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "package.json"), "w", encoding="utf-8") as f:
        f.write(f'{{"name": "{name}", "version": "0.1.0"}}\n')


def _yaml_load(text):
    return YAML(typ="safe").load(text)


class TestAutoDetectDepthZeroDual:
    """(a) Co-located manifests yield both targets at the same path."""

    def test_auto_detect_returns_both_pypi_and_npm(self, tmp_path):
        d = str(tmp_path / "member")
        _write_pyproject(d)
        _write_package_json(d)

        entries = _auto_detect(d)
        names = {e.name for e in entries}
        assert names == {"pypi", "npm"}
        # Both targets are pinned to the dir itself (depth-zero path).
        assert all(e.path == d for e in entries)

    def test_detect_targets_no_config_matches_auto_detect(self, tmp_path):
        """With no .rlsbl/config.json, detect_targets falls through to
        auto-detection and returns the same dual result."""
        d = str(tmp_path / "member")
        _write_pyproject(d)
        _write_package_json(d)

        entries = detect_targets(d)
        names = {e.name for e in entries}
        assert names == {"pypi", "npm"}
        assert all(e.path == d for e in entries)


class TestAutoDetectIgnoresSubdirManifest:
    """(b) A subdir manifest is invisible to auto-detect; only a config
    ``path`` entry reaches it."""

    def test_auto_detect_does_not_descend_into_npm_subdir(self, tmp_path):
        d = str(tmp_path / "member")
        os.makedirs(d, exist_ok=True)
        # Manifest lives one level down, in npm/.
        _write_package_json(os.path.join(d, "npm"))

        entries = _auto_detect(d)
        assert entries == []

    def test_parse_target_entry_joins_subdir_path(self, tmp_path):
        d = str(tmp_path / "member")
        te = _parse_target_entry({"name": "npm", "path": "npm/"}, d)
        assert te == TargetEntry(name="npm", path=os.path.join(d, "npm/"))

    def test_config_declared_path_entry_resolves_subdir(self, tmp_path):
        d = str(tmp_path / "member")
        os.makedirs(os.path.join(d, ".rlsbl"), exist_ok=True)
        _write_package_json(os.path.join(d, "npm"))
        with open(os.path.join(d, ".rlsbl", "config.json"), "w", encoding="utf-8") as f:
            f.write('{"targets": [{"name": "npm", "path": "npm/"}]}\n')

        entries = detect_targets(d)
        assert len(entries) == 1
        assert entries[0].name == "npm"
        assert entries[0].path == os.path.join(d, "npm/")


# Minimal template vars covering both pypi and npm publish templates. Any
# placeholder these do not resolve is sheltered/dropped by
# _generate_merged_publish, which does not affect the structural assertions.
_TEMPLATE_VARS = {
    "repoName": "user/repo",
    "name": "dual",
    "version": "0.1.0",
    "npm.registryUrl": "https://registry.npmjs.org",
    "npm.name": "dual",
    "npm.version": "0.1.0",
    "npm.repoName": "user/repo",
    "npm.binCommand": "dual",
    "npm.author": "",
    "npm.packageManager": "npm",
    "pypi.name": "dual",
    "pypi.version": "0.1.0",
    "pypi.repoName": "user/repo",
    "pypi.minRequiredPython": "3.11",
}


class TestRouterDualTargetMember:
    """(c) The router inlines all of a dual-target member's publish jobs,
    wired to one gate and one shared tag condition."""

    def _build_member(self, tmp_path):
        root = str(tmp_path)
        member_dir = os.path.join(root, "dual")
        _write_pyproject(member_dir)
        _write_package_json(member_dir)

        # Generate the member's publish.yml with BOTH pypi and npm jobs rather
        # than hand-authoring it.
        publish_yml = _generate_merged_publish(["pypi", "npm"], _TEMPLATE_VARS)
        wf_dir = os.path.join(member_dir, ".github", "workflows")
        os.makedirs(wf_dir, exist_ok=True)
        with open(os.path.join(wf_dir, "publish.yml"), "w", encoding="utf-8") as f:
            f.write(publish_yml)
        return root

    def test_both_member_jobs_share_gate_and_condition(self, tmp_path):
        root = self._build_member(tmp_path)
        projects = [{"name": "dual", "path": "dual", "_ci_files": ["dual-ci.yml"]}]

        result = generate_inline_publish_router(projects, root)
        doc = _yaml_load(result)
        jobs = doc["jobs"]

        # Both member publish jobs are present (npm + pypi, prefixed).
        assert "dual-pypi" in jobs
        assert "dual-npm" in jobs

        # Exactly one gate job, and both member jobs need it.
        gate_keys = [k for k in jobs if k.endswith("gate")]
        assert gate_keys == [GATE_JOB_KEY]
        for key in ("dual-pypi", "dual-npm"):
            needs = jobs[key].get("needs")
            needs_list = [needs] if isinstance(needs, str) else (needs or [])
            assert GATE_JOB_KEY in needs_list, f"{key} must need the gate"

        # Both jobs share the exact same ref-based tag condition.
        cond_pypi = jobs["dual-pypi"]["if"]
        cond_npm = jobs["dual-npm"]["if"]
        assert cond_pypi == cond_npm
        assert cond_pypi == (
            "startsWith(inputs.tag || github.ref_name, 'dual@v')"
        )
