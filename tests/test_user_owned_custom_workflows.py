"""Tests for the user-owned ci-custom.yml / publish-custom.yml workflow pattern.

These files are NEVER created by scaffold and NEVER overwritten. They
exist as the escape hatch for users who want to add jobs to CI without fighting
the three-way merge on the scaffold-managed ci.yml/publish.yml.
"""

import os

from rlsbl.commands.init_cmd import (
    USER_OWNED,
    _finalize_scaffold,
    process_mappings,
)


# ---------------------------------------------------------------------------
# Task 8.1: USER_OWNED membership
# ---------------------------------------------------------------------------


def test_ci_custom_in_user_owned():
    """ci-custom.yml must be in the USER_OWNED set."""
    assert ".github/workflows/ci-custom.yml" in USER_OWNED


def test_publish_custom_in_user_owned():
    """publish-custom.yml must be in the USER_OWNED set."""
    assert ".github/workflows/publish-custom.yml" in USER_OWNED


# ---------------------------------------------------------------------------
# Task 8.1: scaffold never touches ci-custom.yml
# ---------------------------------------------------------------------------


def test_scaffold_update_does_not_touch_ci_custom(tmp_project):
    """If a user creates ci-custom.yml, scaffold must leave it alone.

    We simulate a scaffold by running process_mappings with a template targeting
    .github/workflows/ci-custom.yml -- the USER_OWNED guard should kick in
    regardless of where the mapping comes from.
    """
    target = ".github/workflows/ci-custom.yml"
    target_path = tmp_project / target
    target_path.parent.mkdir(parents=True, exist_ok=True)
    user_content = (
        "name: CI (custom)\n"
        "\n"
        "on:\n"
        "  push:\n"
        "\n"
        "jobs:\n"
        "  user-job:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hello\n"
    )
    target_path.write_text(user_content)

    # A hypothetical template trying to overwrite the custom file
    tpl_dir = tmp_project / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "ci-custom.yml.tpl").write_text("name: SHOULD NOT APPEAR\n")

    mappings = [{"template": "ci-custom.yml.tpl", "target": target}]

    created, skipped, warnings, _ = process_mappings(
        str(tpl_dir), mappings, {}, force=False,
    )

    # Content untouched
    assert target_path.read_text() == user_content
    skipped_targets = [t for t, _ in skipped]
    assert target in skipped_targets
    created_targets = [t for t, _ in created]
    assert target not in created_targets


def test_scaffold_force_does_not_touch_ci_custom(tmp_project):
    """USER_OWNED takes precedence even over --force for ci-custom.yml."""
    target = ".github/workflows/ci-custom.yml"
    target_path = tmp_project / target
    target_path.parent.mkdir(parents=True, exist_ok=True)
    user_content = "name: CI (custom)\n# my stuff\n"
    target_path.write_text(user_content)

    tpl_dir = tmp_project / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "ci-custom.yml.tpl").write_text("name: OVERWRITTEN\n")

    mappings = [{"template": "ci-custom.yml.tpl", "target": target}]

    created, skipped, warnings, _ = process_mappings(
        str(tpl_dir), mappings, {}, force=True,
    )

    assert target_path.read_text() == user_content
    skipped_targets = [t for t, _ in skipped]
    assert target in skipped_targets


def test_scaffold_update_does_not_touch_publish_custom(tmp_project):
    """Same behavior for publish-custom.yml as for ci-custom.yml."""
    target = ".github/workflows/publish-custom.yml"
    target_path = tmp_project / target
    target_path.parent.mkdir(parents=True, exist_ok=True)
    user_content = "name: Publish (custom)\n"
    target_path.write_text(user_content)

    tpl_dir = tmp_project / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "publish-custom.yml.tpl").write_text("name: OVERWRITTEN\n")

    mappings = [{"template": "publish-custom.yml.tpl", "target": target}]

    created, skipped, warnings, _ = process_mappings(
        str(tpl_dir), mappings, {}, force=False,
    )

    assert target_path.read_text() == user_content
    skipped_targets = [t for t, _ in skipped]
    assert target in skipped_targets


# ---------------------------------------------------------------------------
# Task 8.2: conflict tip printed when ci.yml/publish.yml has conflicts
# ---------------------------------------------------------------------------


def test_conflict_tip_printed_for_ci_yml(mock_git_repo, capsys):
    """When ci.yml ends up CONFLICTS, the tip pointing at ci-custom.yml prints."""
    ci_path = ".github/workflows/ci.yml"
    full_path = mock_git_repo / ci_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text("<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\n")

    _finalize_scaffold(
        existing_hashes={},
        all_hash_dicts=[{}],
        created=[(ci_path, "CONFLICTS -- resolve manually")],
        skipped=[],
        warnings=[f"{ci_path}: merge conflicts detected, resolve manually"],
        registry=None,
        flags={"auto-tag": False, "auto-commit": False},
        registries=[],
        project_root=".",
        config={})

    captured = capsys.readouterr()
    assert "ci-custom.yml" in captured.out
    assert "scaffold never touches this file" in captured.out


def test_conflict_tip_printed_for_publish_yml(mock_git_repo, capsys):
    """When publish.yml ends up CONFLICTS, the tip points at publish-custom.yml."""
    pub_path = ".github/workflows/publish.yml"
    full_path = mock_git_repo / pub_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text("<<<<<<<\n=======\n>>>>>>>\n")

    _finalize_scaffold(
        existing_hashes={},
        all_hash_dicts=[{}],
        created=[(pub_path, "CONFLICTS -- resolve manually")],
        skipped=[],
        warnings=[f"{pub_path}: merge conflicts detected, resolve manually"],
        registry=None,
        flags={"auto-tag": False, "auto-commit": False},
        registries=[],
        project_root=".",
        config={})

    captured = capsys.readouterr()
    assert "publish-custom.yml" in captured.out


def test_conflict_tip_not_printed_when_no_conflict(mock_git_repo, capsys):
    """No conflict on ci.yml/publish.yml -> no tip printed."""
    ci_path = ".github/workflows/ci.yml"
    full_path = mock_git_repo / ci_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text("name: CI\n")

    _finalize_scaffold(
        existing_hashes={},
        all_hash_dicts=[{}],
        created=[(ci_path, "merged")],
        skipped=[],
        warnings=[],
        registry=None,
        flags={"auto-tag": False, "auto-commit": False},
        registries=[],
        project_root=".",
        config={})

    captured = capsys.readouterr()
    # The tip text is unique enough to assert its absence.
    assert "scaffold never touches this file" not in captured.out


def test_conflict_tip_not_printed_for_other_conflicts(mock_git_repo, capsys):
    """A conflict on some other file (e.g., CONVENTIONS.md) doesn't trigger the workflow tip."""
    other = "CONVENTIONS.md"
    (mock_git_repo / other).write_text("<<<<<<< ours\n=======\n>>>>>>> theirs\n")

    _finalize_scaffold(
        existing_hashes={},
        all_hash_dicts=[{}],
        created=[(other, "CONFLICTS -- resolve manually")],
        skipped=[],
        warnings=[f"{other}: merge conflicts detected, resolve manually"],
        registry=None,
        flags={"auto-tag": False, "auto-commit": False},
        registries=[],
        project_root=".",
        config={})

    captured = capsys.readouterr()
    assert "ci-custom.yml" not in captured.out
    assert "publish-custom.yml" not in captured.out
