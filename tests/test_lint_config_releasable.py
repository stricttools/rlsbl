"""Tests for Phase 5.4: two-level lint config resolution.

Lint config resolves from the member-level .rlsbl/lint/<language>.toml when it
exists, otherwise from the releasable-level lint/ directory (member wins
wholesale per file). This mirrors the config.json 2-level precedent.
"""

from rlsbl.lint.config import load_language_config


PY_ALLOW_FLASK = """\
[forbidden-imports]
modules = ["flask", "fastapi"]
allow = ["flask"]
"""

PY_FORBID_ALL = """\
[forbidden-imports]
modules = ["flask", "fastapi"]
"""


def _write(dirpath, name, content):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / name).write_text(content)


class TestTwoLevelLintResolution:
    def test_member_level_used_when_present(self, tmp_path):
        proj = tmp_path / "pkg"
        _write(proj / ".rlsbl" / "lint", "python.toml", PY_ALLOW_FLASK)
        rel_lint = tmp_path / "rel" / "lint"
        _write(rel_lint, "python.toml", PY_FORBID_ALL)

        cfg = load_language_config(str(proj), "python", releasable_lint_dir=str(rel_lint))
        # Member file wins: flask allowed.
        assert "flask" in cfg.allowed_imports

    def test_releasable_level_used_when_member_absent(self, tmp_path):
        proj = tmp_path / "pkg"
        proj.mkdir()
        rel_lint = tmp_path / "rel" / "lint"
        _write(rel_lint, "python.toml", PY_ALLOW_FLASK)

        cfg = load_language_config(str(proj), "python", releasable_lint_dir=str(rel_lint))
        # No member file -> releasable file used: flask allowed.
        assert "flask" in cfg.allowed_imports
        assert cfg.forbidden_imports == ["flask", "fastapi"]

    def test_defaults_when_neither_present(self, tmp_path):
        proj = tmp_path / "pkg"
        proj.mkdir()
        cfg = load_language_config(str(proj), "python", releasable_lint_dir=None)
        # Falls back to per-language defaults.
        assert "flask" in cfg.forbidden_imports
        assert cfg.allowed_imports == []

    def test_no_releasable_dir_uses_member(self, tmp_path):
        proj = tmp_path / "pkg"
        _write(proj / ".rlsbl" / "lint", "python.toml", PY_ALLOW_FLASK)
        cfg = load_language_config(str(proj), "python", releasable_lint_dir=None)
        assert "flask" in cfg.allowed_imports
