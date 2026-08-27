"""Per-axis conformance: name registries and package-name normalization.

Four separate places kept their own copy of "which registries rlsbl can talk
to": a dispatch dict in ``rlsbl.registry``, a normalizer dict in the
name-consistency check, a hard-coded pair in ``rlsbl claim-name`` (restated
twice more inside it for tokens and for the publish routine), and a display
table in ``rlsbl check-name``. All four now ask the target.

The supported side must behave exactly as it did; an unsupported one must
produce a named refusal rather than a silent default.
"""

import pytest

from rlsbl.registry import query_registry_version
from rlsbl.targets import TARGETS, claimable_targets, targets_with_version_queries
from rlsbl.targets.base import BaseTarget
from rlsbl.targets.utils import normalize_go, normalize_npm, normalize_pypi


class TestNormalization:
    """`normalize_package_name` replaces the dict keyed by target name."""

    @pytest.mark.parametrize(
        "target_name,raw,expected_fn",
        [
            ("npm", "My_Cool.Pkg", normalize_npm),
            ("pypi", "My_Cool.Pkg", normalize_pypi),
            ("go", "github.com/User/Repo", normalize_go),
        ],
    )
    def test_registry_rules_are_unchanged(self, target_name, raw, expected_fn):
        assert TARGETS[target_name].normalize_package_name(raw) == expected_fn(raw)

    def test_the_three_rules_actually_differ(self):
        """A shared default would hide real collisions -- pin that they differ."""
        raw = "my_cool.pkg"
        answers = {
            TARGETS[n].normalize_package_name(raw) for n in ("npm", "pypi", "go")
        }
        assert len(answers) > 1

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_every_target_normalizes_without_a_lookup(self, name):
        """No target may be missing from the normalization answer."""
        assert TARGETS[name].normalize_package_name("Foo_Bar") is not None

    def test_the_default_is_lowercasing(self):
        """A registry with no folding rules of its own lowercases."""
        assert TARGETS["zig"].normalize_package_name("Foo-Bar") == "foo-bar"


class TestVersionQueries:
    """`query_registry_version` dispatches through the registry, not a dict."""

    def test_the_query_set_is_derived(self):
        derived = targets_with_version_queries()
        assert derived == frozenset(
            n
            for n, t in TARGETS.items()
            if type(t).query_latest_version is not BaseTarget.query_latest_version
        )
        assert derived == {"npm", "pypi", "go"}

    @pytest.mark.parametrize("registry", ["npm", "pypi", "go"])
    def test_a_supported_registry_reaches_its_query_function(self, registry, monkeypatch):
        seen = {}

        def fake(name):
            seen["name"] = name
            return {"status": "found", "version": "9.9.9"}

        monkeypatch.setattr(
            type(TARGETS[registry]), "query_latest_version",
            lambda self, name: fake(name),
        )
        result = query_registry_version("some-pkg", registry)
        assert seen["name"] == "some-pkg"
        assert result == {"status": "found", "version": "9.9.9"}

    def test_an_unregistered_registry_is_a_named_error(self):
        result = query_registry_version("x", "cobol")
        assert result["status"] == "error"
        assert "Unknown registry: cobol" in result["message"]

    def test_a_registered_target_without_a_version_api_is_a_named_error(self):
        """Not None, and not "not_found" -- those would read as unpublished."""
        result = query_registry_version("x", "zig")
        assert result["status"] == "error"
        assert "Unknown registry: zig" in result["message"]


class TestClaimSelection:
    """`claim-name`'s accepted set and its per-target knowledge are derived."""

    def test_claimable_set_is_derived(self):
        derived = claimable_targets()
        assert derived == frozenset(
            n
            for n, t in TARGETS.items()
            if type(t).claim_placeholder is not BaseTarget.claim_placeholder
        )
        assert derived == {"npm", "pypi"}

    def test_a_non_claimable_target_refuses_naming_itself(self):
        with pytest.raises(NotImplementedError, match="'zig'"):
            TARGETS["zig"].claim_placeholder("x", "/tmp-unused")

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_token_env_vars_are_declared_exactly_for_claimable_targets(self, name):
        target = TARGETS[name]
        declared = bool(target.claim_token_env_vars)
        assert declared == (name in claimable_targets()), (
            f"'{name}' declares claim token env vars but cannot claim, or "
            f"vice versa"
        )

    def test_declared_token_env_vars_are_the_ones_the_command_required(self):
        assert TARGETS["npm"].claim_token_env_vars == ("NPM_TOKEN",)
        assert TARGETS["pypi"].claim_token_env_vars == (
            "PYPI_TOKEN", "UV_PUBLISH_TOKEN",
        )


class TestRegistryDisplayNames:
    """The display table is asked of the targets, plus one declared non-target."""

    def test_targets_supply_their_own_display_names(self):
        from rlsbl.commands.check import _registry_display

        assert _registry_display("npm") == "npm"
        assert _registry_display("pypi") == "PyPI"
        assert _registry_display("go") == "pkg.go.dev"

    def test_github_is_declared_as_the_one_non_target_registry(self):
        from rlsbl.commands.check import (
            _NON_TARGET_REGISTRY_DISPLAY,
            _registry_display,
        )

        assert _registry_display("github") == "GitHub"
        assert set(_NON_TARGET_REGISTRY_DISPLAY) & set(TARGETS) == set()

    def test_an_unknown_registry_renders_as_itself(self):
        from rlsbl.commands.check import _registry_display

        assert _registry_display("wat") == "wat"
