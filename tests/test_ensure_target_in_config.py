"""Tests for _ensure_target_in_config: it must read current targets from the
on-disk per-project config.json, never from the (possibly empty) in-memory
ctx.config, and never downgrade structured target entries to plain strings."""

import json
import os

from rlsbl.commands.init_cmd import _ensure_target_in_config
from rlsbl.context import ProjectContext


def _write_config(project_root, config):
    rlsbl_dir = os.path.join(str(project_root), ".rlsbl")
    os.makedirs(rlsbl_dir, exist_ok=True)
    path = os.path.join(rlsbl_dir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    return path


def _read_config(project_root):
    path = os.path.join(str(project_root), ".rlsbl", "config.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _bare_ctx(project_root, config=None):
    """Build a ProjectContext with an (intentionally empty) in-memory config,
    mirroring bare-CLI / test scenarios where config was never loaded."""
    return ProjectContext(
        project_root=project_root,
        workspace_root=None,
        config=config if config is not None else {},
    )


class TestPreservesStructuredEntries:
    def test_structured_entry_survives_when_already_present(self, tmp_path):
        """A structured {"name": "go", "path": "go/"} entry on disk must NOT be
        clobbered to a plain string when ctx.config is empty and the registry
        is already present."""
        _write_config(tmp_path, {"targets": [{"name": "go", "path": "go/"}]})
        raw_before = (tmp_path / ".rlsbl" / "config.json").read_text()

        ctx = _bare_ctx(tmp_path)  # empty in-memory config
        _ensure_target_in_config("go", ctx)

        # Structured entry preserved exactly; nothing was written.
        assert _read_config(tmp_path)["targets"] == [{"name": "go", "path": "go/"}]
        raw_after = (tmp_path / ".rlsbl" / "config.json").read_text()
        assert raw_after == raw_before, "file should not be rewritten when nothing changed"

    def test_new_registry_appended_preserving_structured(self, tmp_path):
        """Adding a new registry appends a plain string while leaving the
        existing structured entry untouched."""
        _write_config(tmp_path, {"targets": [{"name": "go", "path": "go/"}]})

        ctx = _bare_ctx(tmp_path)
        _ensure_target_in_config("npm", ctx)

        targets = _read_config(tmp_path)["targets"]
        assert {"name": "go", "path": "go/"} in targets
        assert "npm" in targets
        assert len(targets) == 2

    def test_registry_present_as_string_is_noop(self, tmp_path):
        _write_config(tmp_path, {"targets": ["go", "npm"]})
        raw_before = (tmp_path / ".rlsbl" / "config.json").read_text()

        ctx = _bare_ctx(tmp_path)
        _ensure_target_in_config("go", ctx)

        assert _read_config(tmp_path)["targets"] == ["go", "npm"]
        raw_after = (tmp_path / ".rlsbl" / "config.json").read_text()
        assert raw_after == raw_before

    def test_present_as_dict_not_re_added_as_string(self, tmp_path):
        """When the registry is present only in structured (dict) form, it must
        not be re-appended as a plain string."""
        _write_config(tmp_path, {"targets": [{"name": "go", "path": "go/"}]})
        ctx = _bare_ctx(tmp_path)
        _ensure_target_in_config("go", ctx)
        targets = _read_config(tmp_path)["targets"]
        assert targets == [{"name": "go", "path": "go/"}]

    def test_empty_disk_targets_appends_string(self, tmp_path):
        _write_config(tmp_path, {"publish_mode": "ci"})  # no targets key
        ctx = _bare_ctx(tmp_path)
        _ensure_target_in_config("pypi", ctx)
        assert _read_config(tmp_path)["targets"] == ["pypi"]


class TestCtxConfigRefreshedAfterCall:
    def test_ctx_config_reflects_disk_after_add(self, tmp_path):
        """After a write, ctx.config is refreshed from disk (fresh read)."""
        _write_config(tmp_path, {"targets": [{"name": "go", "path": "go/"}]})
        ctx = _bare_ctx(tmp_path)
        _ensure_target_in_config("npm", ctx)
        assert "npm" in ctx.config["targets"]
        assert {"name": "go", "path": "go/"} in ctx.config["targets"]

    def test_ctx_config_reflects_disk_when_noop(self, tmp_path):
        _write_config(tmp_path, {"targets": [{"name": "go", "path": "go/"}]})
        ctx = _bare_ctx(tmp_path)
        _ensure_target_in_config("go", ctx)
        assert ctx.config["targets"] == [{"name": "go", "path": "go/"}]
