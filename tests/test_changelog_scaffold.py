"""Tests for scaffold support of JSONL changelog directory."""

import os
from pathlib import Path

from rlsbl.commands.init_cmd import USER_OWNED, process_mappings


def test_unreleased_jsonl_in_user_owned():
    """The unreleased.jsonl file should be in USER_OWNED so scaffold skips it."""
    assert ".rlsbl/changes/unreleased.jsonl" in USER_OWNED


def test_unreleased_jsonl_template_exists():
    """The template file for unreleased.jsonl must exist and be empty."""
    tpl_path = (
        Path(__file__).resolve().parent.parent
        / "rlsbl"
        / "templates"
        / "shared"
        / "changes"
        / "unreleased.jsonl.tpl"
    )
    assert tpl_path.exists(), f"Template not found: {tpl_path}"
    assert tpl_path.read_text() == ""


def test_unreleased_jsonl_in_shared_mappings():
    """shared_template_mappings() must include the unreleased.jsonl mapping."""
    from rlsbl.targets.base import BaseTarget

    mappings = BaseTarget().shared_template_mappings(None)
    targets = {m["target"] for m in mappings}
    assert ".rlsbl/changes/unreleased.jsonl" in targets


def test_scaffold_creates_unreleased_jsonl(tmp_project):
    """Scaffold on a new project creates .rlsbl/changes/unreleased.jsonl as an empty file."""
    from rlsbl.targets.base import BaseTarget

    base = BaseTarget()
    tpl_dir = base.shared_template_dir()
    mappings = [m for m in base.shared_template_mappings(None)
                if m["target"] == ".rlsbl/changes/unreleased.jsonl"]

    created, skipped, warnings, _ = process_mappings(
        tpl_dir, mappings, {},
    )

    target = tmp_project / ".rlsbl" / "changes" / "unreleased.jsonl"
    assert target.exists()
    assert target.read_text() == ""
    created_targets = [t for t, _ in created]
    assert ".rlsbl/changes/unreleased.jsonl" in created_targets


def test_scaffold_update_does_not_overwrite_unreleased_jsonl(tmp_project):
    """scaffold must not overwrite unreleased.jsonl (it's user-owned)."""
    from rlsbl.targets.base import BaseTarget

    # Create the file with some existing entries
    target_dir = tmp_project / ".rlsbl" / "changes"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "unreleased.jsonl"
    user_content = '{"type":"added","description":"my feature"}\n'
    target_file.write_text(user_content)

    base = BaseTarget()
    tpl_dir = base.shared_template_dir()
    mappings = [m for m in base.shared_template_mappings(None)
                if m["target"] == ".rlsbl/changes/unreleased.jsonl"]

    created, skipped, warnings, _ = process_mappings(
        tpl_dir, mappings, {},
    )

    # File must be unchanged
    assert target_file.read_text() == user_content
    skipped_targets = [t for t, _ in skipped]
    assert ".rlsbl/changes/unreleased.jsonl" in skipped_targets
    created_targets = [t for t, _ in created]
    assert ".rlsbl/changes/unreleased.jsonl" not in created_targets


def test_scaffold_force_does_not_overwrite_unreleased_jsonl(tmp_project):
    """scaffold --force must not overwrite unreleased.jsonl (USER_OWNED takes precedence)."""
    from rlsbl.targets.base import BaseTarget

    target_dir = tmp_project / ".rlsbl" / "changes"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "unreleased.jsonl"
    user_content = '{"type":"fixed","description":"a bug fix"}\n'
    target_file.write_text(user_content)

    base = BaseTarget()
    tpl_dir = base.shared_template_dir()
    mappings = [m for m in base.shared_template_mappings(None)
                if m["target"] == ".rlsbl/changes/unreleased.jsonl"]

    created, skipped, warnings, _ = process_mappings(
        tpl_dir, mappings, {},
    )

    # User-owned files are never overwritten (USER_OWNED takes precedence)
    assert target_file.read_text() == user_content
    skipped_targets = [t for t, _ in skipped]
    assert ".rlsbl/changes/unreleased.jsonl" in skipped_targets
