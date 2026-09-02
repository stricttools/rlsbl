"""Direct unit tests for ``derive_releasable_tag_format``.

The tag format an auto-singleton releasable is created with is one fact with
one answer, shared by the two commands that create a releasable from a member
(``monorepo add`` and ``monorepo absorb``). Both reach it through this
function, so its four outcomes are asserted here rather than only through the
commands: the @-scheme, Go's path scheme, the mixed-scheme refusal, and the
answer for a member whose targets are none this rlsbl recognizes.
"""

import pytest

from rlsbl.errors import MixedTagSchemeError
from rlsbl.tag_glob import DEFAULT_TAG_FORMAT, derive_releasable_tag_format
from rlsbl.targets import TargetEntry


def _derive(entries, name="thing", path="pkgs/thing"):
    return derive_releasable_tag_format(
        entries, name, path, subject=f"member dir '{path}'",
    )


class TestDerivedFormat:
    def test_an_at_style_target_derives_the_workspace_scheme(self):
        assert _derive([TargetEntry(name="npm", path="pkgs/thing")]) == "{name}@v{version}"
        assert DEFAULT_TAG_FORMAT == "{name}@v{version}"

    def test_several_at_style_targets_agree(self):
        entries = [
            TargetEntry(name="npm", path="pkgs/thing"),
            TargetEntry(name="pypi", path="pkgs/thing"),
        ]
        assert _derive(entries) == "{name}@v{version}"

    def test_a_go_target_derives_the_module_proxy_path_scheme(self):
        entries = [TargetEntry(name="go", path="pkgs/thing")]
        assert _derive(entries) == "pkgs/thing/v{version}"

    def test_the_path_scheme_follows_the_member_path(self):
        entries = [TargetEntry(name="go", path="libs/inner/widget")]
        derived = _derive(entries, name="widget", path="libs/inner/widget")
        assert derived == "libs/inner/widget/v{version}"

    def test_the_primary_target_decides(self):
        """The first RECOGNIZED entry answers; an unknown one is stepped over."""
        entries = [
            TargetEntry(name="not-a-real-target", path="pkgs/thing"),
            TargetEntry(name="go", path="pkgs/thing"),
        ]
        assert _derive(entries) == "pkgs/thing/v{version}"


class TestNoRecognizedTarget:
    def test_no_targets_at_all_answers_the_workspace_scheme(self):
        assert _derive([]) == DEFAULT_TAG_FORMAT

    def test_only_unrecognized_targets_answer_the_workspace_scheme(self):
        entries = [TargetEntry(name="not-a-real-target", path="pkgs/thing")]
        assert _derive(entries) == DEFAULT_TAG_FORMAT


class TestMixedSchemesRefuse:
    def test_both_schemes_raise_naming_both_formats_and_the_flag(self):
        entries = [
            TargetEntry(name="go", path="pkgs/thing"),
            TargetEntry(name="npm", path="pkgs/thing"),
        ]
        with pytest.raises(MixedTagSchemeError) as excinfo:
            _derive(entries)
        message = str(excinfo.value)
        # Both candidate formats, so the operator can copy one out.
        assert "{name}@v{version}" in message
        assert "pkgs/thing/v{version}" in message
        assert "--tag-format" in message
        # Both offending target names, and the subject that declared them.
        assert "go" in message
        assert "npm" in message
        assert "member dir 'pkgs/thing'" in message

    def test_the_refusal_does_not_depend_on_detection_order(self):
        entries = [
            TargetEntry(name="npm", path="pkgs/thing"),
            TargetEntry(name="go", path="pkgs/thing"),
        ]
        with pytest.raises(MixedTagSchemeError):
            _derive(entries)
