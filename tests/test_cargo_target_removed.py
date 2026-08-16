"""Regression tests for the removal of the cargo / crates.io support.

The cargo release target, the cargo publish pipeline, the crates.io registry
branches (check-name / claim-name / yank / version query), the crates-wrapper
launcher for Go binaries, and the cargo CI/publish templates were all deleted.

Rust projects are simply not an rlsbl release target. These tests pin the
removal so none of it silently returns.
"""

import re
from pathlib import Path

import pytest

import rlsbl
from rlsbl.pipelines import PIPELINE_TYPES
from rlsbl.registry import _REGISTRY_DISPATCH
from rlsbl.targets import TARGETS
from conftest import cli_ctx

app = rlsbl.app

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "rlsbl"


class TestRegistriesDeregistered:

    def test_cargo_target_gone(self):
        assert "cargo" not in TARGETS

    def test_cargo_pipeline_gone(self):
        assert "cargo" not in PIPELINE_TYPES

    def test_crates_registry_query_gone(self):
        assert "cargo" not in _REGISTRY_DISPATCH
        assert "crates" not in _REGISTRY_DISPATCH


class TestModulesDeleted:

    @pytest.mark.parametrize("relpath", [
        "targets/cargo.py",
        "pipelines/cargo.py",
        "crates_wrapper.py",
        "templates/cargo",
        "templates/shared/crates-wrapper",
    ])
    def test_module_or_template_deleted(self, relpath):
        assert not (PKG_ROOT / relpath).exists(), f"{relpath} must stay deleted"

    @pytest.mark.parametrize("modname", [
        "rlsbl.targets.cargo",
        "rlsbl.pipelines.cargo",
        "rlsbl.crates_wrapper",
    ])
    def test_module_not_importable(self, modname):
        with pytest.raises(ModuleNotFoundError):
            __import__(modname)

    def test_build_cargo_assets_gone(self):
        from rlsbl.pipelines import build

        assert not hasattr(build, "build_cargo_assets")
        assert not hasattr(build, "_read_cargo_name")


class TestCliSurface:

    def test_check_name_rejects_crates(self):
        """`--target crates` is refused by the declaration, not by the handler.

        The registry list is `choices=[Choice("npm"), ...]` since the strictcli
        0.41 migration, so the refusal happens at parse time and names every
        legal value.
        """
        result = app.test(["check-name", "serde", "--target", "crates"])
        assert result.exit_code == 1
        assert "crates" in result.stderr
        assert "npm, pypi, go, github" in result.stderr

    def test_claim_name_rejects_crates(self):
        """`claim-name --target crates` is likewise a parse-time refusal."""
        result = app.test(["claim-name", "serde", "--target", "crates"])
        assert result.exit_code == 1
        assert "crates" in result.stderr
        assert "npm, pypi" in result.stderr

    def test_check_name_help_does_not_offer_crates(self):
        result = app.test(["check-name", "--help"])
        assert result.exit_code == 0, result.stderr
        assert "crates" not in result.stdout.lower()


class TestNoSourceReferences:

    def test_no_cargo_or_crates_in_production_code(self):
        pattern = re.compile(r"cargo|crates", re.IGNORECASE)
        # Cargo.toml stays in the plain target's manifest-detection list and in
        # the gitignore template: those are "this repo contains Rust" signals,
        # not a cargo release target. Nothing else may mention it.
        allowed = {
            PKG_ROOT / "targets" / "plain.py",
            PKG_ROOT / "templates" / "shared" / "gitignore.tpl",
        }
        offenders = []
        for path in sorted(PKG_ROOT.rglob("*")):
            if not path.is_file() or path in allowed:
                continue
            if path.suffix not in {".py", ".toml", ".tpl", ".sh", ".yml", ".json"}:
                continue
            for lineno, line in enumerate(
                path.read_text(errors="replace").splitlines(), 1
            ):
                if pattern.search(line):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()[:120]}"
                    )
        assert not offenders, (
            "cargo/crates references remain in production code:\n"
            + "\n".join(offenders)
        )
