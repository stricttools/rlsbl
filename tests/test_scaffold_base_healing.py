"""Tests for scaffold merge-base healing and the removal of --force-overwrite.

Covers the base-tracking-migration incident: repos scaffolded before merge-base
tracking have no stored base, so a re-scaffold used to skip merging and advise
`scaffold --force`, which wholesale-overwrote managed files and destroyed local
edits. The healing path reconstructs each missing base from the file's last
`rlsbl scaffold` commit and runs a real three-way merge instead.
"""

import inspect
import json
import os

import pytest

from githarness import commit_file, init_repo

import rlsbl
from rlsbl.commands.init_cmd import (
    BASES_DIR,
    MANAGED_FILES,
    _load_base,
    _reconstruct_base_from_history,
    _require_healable_bases_dir,
    apply_plans,
    plan_mappings,
    process_mappings,
)
from rlsbl.errors import ConfigError


def _tpl_dir(root, name, content):
    """Create a template directory containing one template file."""
    d = root / "_tpls"
    d.mkdir(exist_ok=True)
    (d / name).write_text(content)
    return str(d)


# ---------------------------------------------------------------------------
# Base reconstruction + three-way merge
# ---------------------------------------------------------------------------

def test_non_conflicting_edit_survives_via_reconstructed_base(tmp_path, monkeypatch, capsys):
    """A missing base is healed from history so a local edit survives the merge."""
    repo = tmp_path / "repo"
    init_repo(repo)
    target = ".github/workflows/ci.yml"
    # Last scaffold committed the base under an 'rlsbl scaffold' message.
    commit_file(repo, target, "a\nb\nc\n", "rlsbl scaffold")
    # User edits a different region locally; no stored base on disk.
    (repo / target).write_text("aX\nb\nc\n")
    tpl = _tpl_dir(repo, "ci.tpl", "a\nb\nc\nd\n")  # template appended 'd'
    monkeypatch.chdir(repo)

    mappings = [{"template": "ci.tpl", "target": target}]
    created, skipped, warnings, _ = apply_plans(plan_mappings(tpl, mappings, {}))

    merged = (repo / target).read_text()
    # A one-line notice reports the reconstruction.
    assert "base reconstructed from last scaffold commit" in capsys.readouterr().out
    # After the merge, the stored base advances to the new template.
    assert os.path.exists(repo / BASES_DIR / target)
    assert _load_base(target) == "a\nb\nc\nd\n"
    # Both the local edit and the template addition survive; no overwrite.
    assert "aX" in merged
    assert merged.strip().endswith("d")
    assert "<<<<<<<" not in merged


def test_conflicting_edit_produces_markers_not_overwrite(tmp_path, monkeypatch):
    """A conflicting local edit yields merge markers, never a wholesale overwrite."""
    repo = tmp_path / "repo"
    init_repo(repo)
    target = "f.txt"
    commit_file(repo, target, "line1\nline2\nline3\n", "rlsbl scaffold")
    (repo / target).write_text("line1\nLOCAL\nline3\n")  # ours edited line2
    tpl = _tpl_dir(repo, "f.tpl", "line1\nTEMPLATE\nline3\n")  # theirs edited line2
    monkeypatch.chdir(repo)

    mappings = [{"template": "f.tpl", "target": target}]
    created, skipped, warnings, _ = apply_plans(plan_mappings(tpl, mappings, {}))

    content = (repo / target).read_text()
    assert "<<<<<<<" in content
    # Neither side's content is destroyed.
    assert "LOCAL" in content
    assert "TEMPLATE" in content
    assert any("CONFLICT" in s for _, s in created)


def test_no_scaffold_commit_hard_errors(tmp_path, monkeypatch):
    """No stored base and no scaffold commit to heal from -> hard error."""
    repo = tmp_path / "repo"
    init_repo(repo)
    target = "g.txt"
    (repo / target).write_text("divergent local content\n")  # never committed
    tpl = _tpl_dir(repo, "g.tpl", "template content\n")
    monkeypatch.chdir(repo)

    mappings = [{"template": "g.tpl", "target": target}]
    with pytest.raises(ConfigError) as exc:
        process_mappings(tpl, mappings, {})
    msg = str(exc.value)
    assert "g.txt" in msg               # names the offending file
    assert "delete" in msg              # remediation 1: delete + rescaffold
    assert "rlsbl scaffold" in msg      # remediation 2: commit under scaffold msg


def test_plan_mappings_does_not_write_base_during_analysis(tmp_path, monkeypatch):
    """plan_mappings must stay side-effect-free: no base written during analysis.

    The base reconstruction is read-only during planning; the actual write is
    deferred to apply_plans, so a dry-run scaffold never touches disk.
    """
    repo = tmp_path / "repo"
    init_repo(repo)
    target = "d.txt"
    commit_file(repo, target, "base\n", "rlsbl scaffold")
    (repo / target).write_text("base-edited\n")  # diverged local edit
    tpl = _tpl_dir(repo, "d.tpl", "base\nplus\n")
    monkeypatch.chdir(repo)

    mappings = [{"template": "d.tpl", "target": target}]
    plans = plan_mappings(tpl, mappings, {})  # analysis only -- no apply
    # No base file written yet; the plan carries the heal notice for apply_plans.
    assert not os.path.exists(repo / BASES_DIR / target)
    assert any(p.get("heal_notice") for p in plans)


def test_identical_content_seeds_base_without_history(tmp_path, monkeypatch):
    """When the file already matches the template, seed the base (no error)."""
    repo = tmp_path / "repo"
    init_repo(repo)
    target = "h.txt"
    (repo / target).write_text("same\n")  # never committed
    tpl = _tpl_dir(repo, "h.tpl", "same\n")
    monkeypatch.chdir(repo)

    mappings = [{"template": "h.tpl", "target": target}]
    created, skipped, warnings, _ = process_mappings(tpl, mappings, {})
    assert os.path.exists(repo / BASES_DIR / target)
    assert any(t == target for t, _ in skipped)


# ---------------------------------------------------------------------------
# _reconstruct_base_from_history helper
# ---------------------------------------------------------------------------

def test_reconstruct_finds_last_scaffold_commit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "x.txt", "v1\n", "rlsbl scaffold")
    commit_file(repo, "x.txt", "v2\n", "unrelated edit")
    monkeypatch.chdir(repo)
    result = _reconstruct_base_from_history("x.txt")
    assert result is not None
    content, short_sha = result
    # Reconstructs the SCAFFOLD-commit content, not the latest content.
    assert content == "v1\n"
    assert len(short_sha) == 9


def test_reconstruct_returns_none_without_scaffold_commit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "y.txt", "hi\n", "just a normal commit")
    monkeypatch.chdir(repo)
    assert _reconstruct_base_from_history("y.txt") is None


# ---------------------------------------------------------------------------
# Whole-bases-directory guard
# ---------------------------------------------------------------------------

def test_missing_bases_dir_hard_errors(tmp_project):
    """managed-files.json present but .rlsbl/bases/ absent -> upfront hard error."""
    os.makedirs(os.path.dirname(MANAGED_FILES), exist_ok=True)
    with open(MANAGED_FILES, "w") as f:
        json.dump({"version": 1, "files": {}}, f)
    assert not os.path.isdir(BASES_DIR)

    with pytest.raises(ConfigError) as exc:
        _require_healable_bases_dir()
    msg = str(exc.value)
    assert BASES_DIR in msg
    assert "mkdir" in msg               # tells operator how to opt into healing
    assert "rlsbl scaffold" in msg


def test_fresh_project_bypasses_bases_dir_guard(tmp_project):
    """A first-ever scaffold (no managed-files.json) must not trip the guard."""
    assert not os.path.exists(MANAGED_FILES)
    # Should not raise.
    _require_healable_bases_dir()


def test_bases_dir_present_bypasses_guard(tmp_project):
    """When the bases directory exists, per-file healing handles missing bases."""
    os.makedirs(os.path.dirname(MANAGED_FILES), exist_ok=True)
    with open(MANAGED_FILES, "w") as f:
        json.dump({"version": 1, "files": {}}, f)
    os.makedirs(BASES_DIR, exist_ok=True)
    # Should not raise.
    _require_healable_bases_dir()


# ---------------------------------------------------------------------------
# --force-overwrite removal
# ---------------------------------------------------------------------------

def test_scaffold_has_no_force_overwrite_flag():
    """The scaffold handler no longer accepts a force-overwrite parameter."""
    params = inspect.signature(rlsbl.cmd_scaffold).parameters
    assert "force_overwrite" not in params
    assert "force" not in params


def test_plan_mappings_has_no_force_parameter():
    """The internal merge planner no longer carries a dead force parameter."""
    params = inspect.signature(plan_mappings).parameters
    assert "force" not in params
    params2 = inspect.signature(process_mappings).parameters
    assert "force" not in params2
