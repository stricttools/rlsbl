"""Tests for scaffold extras: --force USER_OWNED behavior, auto-commit, and config migration."""

import os
import subprocess
from pathlib import Path

from rlsbl.commands.init_cmd import (
    USER_OWNED,
    _finalize_scaffold,
    process_mappings,
)


def _write_file(path, content):
    """Helper to write a file, creating parent dirs as needed."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_pre_release_template_is_minimal_stub():
    """pre-release.sh.tpl should be a minimal stub (built-in checks handle tests/lint)."""
    tpl_path = (
        Path(__file__).resolve().parent.parent
        / "rlsbl"
        / "templates"
        / "shared"
        / "hooks"
        / "pre-release.sh.tpl"
    )
    content = tpl_path.read_text()
    assert "Built-in checks" in content
    # Should NOT contain the old test/lint commands
    assert "go vet" not in content
    assert "uv run pytest" not in content
    assert "npm test" not in content


def test_force_does_not_overwrite_changelog(tmp_project):
    """--force must NOT overwrite CHANGELOG.md if it already exists."""
    assert "CHANGELOG.md" in USER_OWNED

    # Pre-existing user content
    changelog = tmp_project / "CHANGELOG.md"
    changelog.write_text("# My Custom Changelog\n\nUser content here.\n")

    # Set up a template directory with a CHANGELOG.md template
    tpl_dir = tmp_project / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "changelog.tpl").write_text("# Changelog\n\nTemplate content.\n")

    mappings = [{"template": "changelog.tpl", "target": "CHANGELOG.md"}]

    created, skipped, warnings, _ = process_mappings(
        str(tpl_dir), mappings, {}, force=True,
    )

    # CHANGELOG.md must be skipped as user-owned, not overwritten
    assert changelog.read_text() == "# My Custom Changelog\n\nUser content here.\n"
    skipped_targets = [t for t, _ in skipped]
    assert "CHANGELOG.md" in skipped_targets
    created_targets = [t for t, _ in created]
    assert "CHANGELOG.md" not in created_targets


def test_force_does_not_overwrite_license(tmp_project):
    """--force must NOT overwrite LICENSE if it already exists (regression for 1a1fc61)."""
    assert "LICENSE" in USER_OWNED

    # Pre-existing user content
    license_file = tmp_project / "LICENSE"
    license_file.write_text("MIT License\n\nCopyright (c) 2025 Custom Author\n")

    # Set up a template directory with a LICENSE template
    tpl_dir = tmp_project / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "license.tpl").write_text("MIT License\n\nCopyright (c) {{year}} {{author}}\n")

    mappings = [{"template": "license.tpl", "target": "LICENSE"}]

    created, skipped, warnings, _ = process_mappings(
        str(tpl_dir), mappings, {"year": "2026", "author": "Template Author"},
        force=True,
    )

    # LICENSE is user-owned (not overwritten with template content),
    # but the copyright year is always updated
    assert license_file.read_text() == "MIT License\n\nCopyright (c) 2025-2026 Custom Author\n"
    created_targets = [t for t, _ in created]
    assert "LICENSE" in created_targets


def test_template_author_never_literal(tmp_project):
    """Template variables like {{author}} must be substituted, never left literal."""
    tpl_dir = tmp_project / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "license.tpl").write_text(
        "MIT License\n\nCopyright (c) 2026 {{author}}\n"
    )

    mappings = [{"template": "license.tpl", "target": "LICENSE"}]

    # With a non-empty author value
    created, skipped, warnings, _ = process_mappings(
        str(tpl_dir), mappings, {"author": "Test User"}, force=False,
    )
    license_file = tmp_project / "LICENSE"
    content = license_file.read_text()
    assert "Test User" in content
    assert "{{author}}" not in content

    # Remove the file and test with empty author -- empty is fine, literal is not
    license_file.unlink()
    created, skipped, warnings, _ = process_mappings(
        str(tpl_dir), mappings, {"author": ""}, force=False,
    )
    content = license_file.read_text()
    assert "{{author}}" not in content


def test_force_overwrites_hooks(tmp_project):
    """--force MUST overwrite hook files (hooks are managed, not user-owned)."""
    hook_path = ".rlsbl/hooks/pre-release.sh"
    assert hook_path not in USER_OWNED

    # Pre-existing user hook
    hook_file = tmp_project / ".rlsbl" / "hooks" / "pre-release.sh"
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    hook_file.write_text("#!/bin/bash\necho 'my custom hook'\n")

    # Set up a template directory with a hook template
    tpl_dir = tmp_project / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "pre-release.sh.tpl").write_text("#!/bin/bash\necho 'template hook'\n")

    mappings = [{"template": "pre-release.sh.tpl", "target": hook_path}]

    created, skipped, warnings, _ = process_mappings(
        str(tpl_dir), mappings, {}, force=True,
    )

    # Hook must be overwritten since it's no longer user-owned
    assert hook_file.read_text() == "#!/bin/bash\necho 'template hook'\n"
    created_targets = [t for t, _ in created]
    assert hook_path in created_targets


def test_force_overwrites_non_user_owned(tmp_project):
    """--force DOES overwrite non-user-owned files that already exist."""
    target = ".github/workflows/ci.yml"
    assert target not in USER_OWNED

    # Pre-existing file with old content
    _write_file(target, "old CI content\n")

    # Template with new content
    tpl_dir = tmp_project / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "ci.yml.tpl").write_text("new CI content from template\n")

    mappings = [{"template": "ci.yml.tpl", "target": target}]

    created, skipped, warnings, _ = process_mappings(
        str(tpl_dir), mappings, {}, force=True,
    )

    # File must be overwritten with template content
    with open(target, "r") as f:
        assert f.read() == "new CI content from template\n"
    created_targets = {t: s for t, s in created}
    assert target in created_targets
    assert created_targets[target] == "overwritten"


# --- Auto-commit tests ---


def test_scaffold_auto_commits_files(mock_git_repo, capsys):
    """After scaffold, created/modified files should be committed (clean tree)."""
    # Set up a template and commit it so it doesn't pollute porcelain output
    tpl_dir = mock_git_repo / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "ci.yml.tpl").write_text("name: CI\n")
    subprocess.run(
        ["git", "add", "templates"], cwd=str(mock_git_repo), check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add test templates"],
        cwd=str(mock_git_repo), check=True,
    )

    mappings = [{"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"}]
    created, skipped, warnings, new_hashes = process_mappings(
        str(tpl_dir), mappings, {}, force=False,
    )

    assert len(created) == 1
    assert created[0][1] == "created"

    # Run _finalize_scaffold which should auto-commit
    # no-tag prevents tagging side effects in test
    _finalize_scaffold(
        existing_hashes={},
        all_hash_dicts=[new_hashes],
        created=created,
        skipped=skipped,
        warnings=warnings,
        registry=None,
        flags={"no-tag": True},
        registries=[],
        project_root=".")

    captured = capsys.readouterr()
    assert "Committed scaffold changes." in captured.out

    # Verify working tree is clean (all scaffold files committed)
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=str(mock_git_repo),
    )
    assert result.stdout.strip() == "", f"Dirty tree: {result.stdout}"


def test_scaffold_no_commit_flag_skips_commit(mock_git_repo, capsys):
    """With --no-commit, scaffold files should remain uncommitted."""
    tpl_dir = mock_git_repo / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "ci.yml.tpl").write_text("name: CI\n")
    subprocess.run(
        ["git", "add", "templates"], cwd=str(mock_git_repo), check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add test templates"],
        cwd=str(mock_git_repo), check=True,
    )

    mappings = [{"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"}]
    created, skipped, warnings, new_hashes = process_mappings(
        str(tpl_dir), mappings, {}, force=False,
    )

    # Run _finalize_scaffold with --no-commit flag
    _finalize_scaffold(
        existing_hashes={},
        all_hash_dicts=[new_hashes],
        created=created,
        skipped=skipped,
        warnings=warnings,
        registry=None,
        flags={"no-commit": True, "no-tag": True},
        registries=[],
        project_root=".")

    captured = capsys.readouterr()
    assert "Skipping commit (--no-commit)." in captured.out
    assert "Committed scaffold changes." not in captured.out

    # Verify files are still uncommitted
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=str(mock_git_repo),
    )
    assert result.stdout.strip() != "", "Tree should be dirty with --no-commit"


def test_pre_push_hook_does_not_pass_args(mock_git_repo, capsys):
    """The installed pre-push hook must not pass $@ to avoid strictcli arg rejection."""
    # Run _finalize_scaffold to install the hook
    _finalize_scaffold(
        existing_hashes={},
        all_hash_dicts=[{}],
        created=[],
        skipped=[],
        warnings=[],
        registry=None,
        flags={"no-commit": True, "no-tag": True},
        registries=[],
        project_root=".")

    hook_path = mock_git_repo / ".git" / "hooks" / "pre-push"
    assert hook_path.exists(), "pre-push hook should be installed"

    content = hook_path.read_text()
    assert '"$@"' not in content, "Hook must not pass $@ (strictcli rejects extra args)"
    assert "'$@'" not in content, "Hook must not pass $@ (strictcli rejects extra args)"
    assert "pre-push-check" in content, "Hook should delegate to pre-push-check"


# --- .npmignore scaffolding tests ---


def test_npm_scaffold_creates_npmignore(tmp_project):
    """Scaffold for npm target should create .npmignore from the template."""
    from rlsbl.targets.npm import NpmTarget

    target = NpmTarget()
    tpl_dir = target.template_dir()
    mappings = target.template_mappings()

    # Verify .npmignore is in the mappings
    npmignore_mappings = [m for m in mappings if m["target"] == ".npmignore"]
    assert len(npmignore_mappings) == 1, ".npmignore should be in npm template_mappings"
    assert npmignore_mappings[0]["template"] == "npmignore.tpl"

    # Process the template and verify the file is created
    created, skipped, warnings, _ = process_mappings(
        tpl_dir, npmignore_mappings, {}, force=False,
    )

    npmignore_path = tmp_project / ".npmignore"
    assert npmignore_path.exists(), ".npmignore should be created by scaffold"
    content = npmignore_path.read_text()
    assert ".rlsbl/" in content, ".npmignore should exclude rlsbl metadata"
    assert "__pycache__/" in content, ".npmignore should exclude Python artifacts"

    created_targets = [t for t, _ in created]
    assert ".npmignore" in created_targets


def test_npmignore_is_user_owned(tmp_project):
    """.npmignore must be in USER_OWNED so scaffold doesn't overwrite it."""
    assert ".npmignore" in USER_OWNED

    # Pre-existing user content
    npmignore = tmp_project / ".npmignore"
    npmignore.write_text("# My custom npmignore\nmy-custom-dir/\n")

    from rlsbl.targets.npm import NpmTarget

    target = NpmTarget()
    tpl_dir = target.template_dir()
    npmignore_mappings = [
        m for m in target.template_mappings() if m["target"] == ".npmignore"
    ]

    # The user-owned file should not be overwritten
    created, skipped, warnings, _ = process_mappings(
        tpl_dir, npmignore_mappings, {}, force=False,
    )

    assert npmignore.read_text() == "# My custom npmignore\nmy-custom-dir/\n"
    skipped_targets = [t for t, _ in skipped]
    assert ".npmignore" in skipped_targets
    created_targets = [t for t, _ in created]
    assert ".npmignore" not in created_targets

    # With --force, the user-owned file should still not be overwritten
    created, skipped, warnings, _ = process_mappings(
        tpl_dir, npmignore_mappings, {}, force=True,
    )

    assert npmignore.read_text() == "# My custom npmignore\nmy-custom-dir/\n"
    skipped_targets = [t for t, _ in skipped]
    assert ".npmignore" in skipped_targets


def test_pypi_scaffold_does_not_create_npmignore(tmp_project):
    """Scaffold for a non-npm target (pypi) should NOT create .npmignore."""
    from rlsbl.targets.pypi import PypiTarget

    target = PypiTarget()
    mappings = target.template_mappings()

    # .npmignore must not appear in pypi's template_mappings
    npmignore_mappings = [m for m in mappings if m["target"] == ".npmignore"]
    assert len(npmignore_mappings) == 0, "pypi target should not include .npmignore"

    # Also check shared_template_mappings -- .npmignore should not be there either
    shared_mappings = target.shared_template_mappings(".")
    shared_npmignore = [m for m in shared_mappings if m["target"] == ".npmignore"]
    assert len(shared_npmignore) == 0, ".npmignore should not be in shared templates"


def test_bare_scaffold_is_idempotent(mock_git_repo):
    """Bare scaffold (no --force) preserves user customizations via three-way merge.

    This proves that bare scaffold does what --update used to do:
    1. Initial scaffold creates a file and stores a merge base
    2. User modifies the scaffolded file (adds a custom line)
    3. Bare scaffold again three-way merges, preserving the custom line
    """
    tpl_dir = mock_git_repo / "_tpls"
    tpl_dir.mkdir()

    # Template v1: a multi-line CI workflow (enough lines for clean merge regions)
    tpl_v1 = (
        "name: CI\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: npm test\n"
    )
    (tpl_dir / "ci.yml.tpl").write_text(tpl_v1)

    target = ".github/workflows/ci.yml"
    mappings = [{"template": "ci.yml.tpl", "target": target}]

    # Step 1: initial scaffold -- creates file and stores base
    created, skipped, warnings, _ = process_mappings(
        str(tpl_dir), mappings, {}, force=False,
    )
    ci_path = mock_git_repo / ".github" / "workflows" / "ci.yml"
    assert ci_path.exists()
    created_targets = [t for t, _ in created]
    assert target in created_targets

    initial_content = ci_path.read_text()
    assert "actions/checkout@v4" in initial_content

    # Step 2: user adds a custom line (non-adjacent to where template will change)
    custom_line = "      # Custom: user-added deployment step"
    user_content = initial_content.rstrip("\n") + "\n" + custom_line + "\n"
    ci_path.write_text(user_content)

    # Step 3: bare scaffold again (no --force) -- template unchanged
    # Should detect no template changes and preserve user content exactly
    created2, skipped2, warnings2, _ = process_mappings(
        str(tpl_dir), mappings, {}, force=False,
    )
    after_content = ci_path.read_text()
    assert custom_line in after_content, (
        "User customization must be preserved when bare scaffold re-runs"
    )

    # Step 4: template changes (v2), bare scaffold should three-way merge
    tpl_v2 = tpl_v1.replace("actions/checkout@v4", "actions/checkout@v5")
    (tpl_dir / "ci.yml.tpl").write_text(tpl_v2)

    created3, skipped3, warnings3, _ = process_mappings(
        str(tpl_dir), mappings, {}, force=False,
    )
    merged_content = ci_path.read_text()

    # Template update applied
    assert "actions/checkout@v5" in merged_content, (
        "Template update must be applied via three-way merge"
    )
    # User customization preserved
    assert custom_line in merged_content, (
        "User customization must survive three-way merge with template update"
    )
    # Verify the merge was reported correctly
    merged_targets = {t: s for t, s in created3}
    assert target in merged_targets
    assert merged_targets[target] == "merged"
