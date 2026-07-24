"""Pinning tests for undo's tag->version extraction (`_version_and_msg`).

`_version_and_msg` reads only ``uc.releasable_name`` and ``uc.monorepo_name``,
so a lightweight namespace stands in for the full undo context. These pin the
accept/reject behavior for realistic release tags across the swap to the shared
``parse_version_tag`` parser.
"""

from types import SimpleNamespace

import pytest

from rlsbl.commands.undo import _version_and_msg


def _uc(releasable_name=None, monorepo_name=None):
    return SimpleNamespace(
        releasable_name=releasable_name, monorepo_name=monorepo_name
    )


class TestVersionAndMsg:
    def test_standalone_final(self):
        # No releasable/monorepo -> tag used verbatim as the message.
        assert _version_and_msg(_uc(), "v1.2.3") == ("1.2.3", "v1.2.3")

    def test_releasable_final(self):
        assert _version_and_msg(_uc(releasable_name="www"), "www@v1.2.3") == (
            "1.2.3", "www: release v1.2.3",
        )

    def test_monorepo_final(self):
        assert _version_and_msg(_uc(monorepo_name="core"), "core@v0.5.0") == (
            "0.5.0", "core: release v0.5.0",
        )

    def test_releasable_prerelease(self):
        assert _version_and_msg(
            _uc(releasable_name="www"), "www@v1.2.3-rc.1"
        ) == ("1.2.3-rc.1", "www: release v1.2.3-rc.1")

    @pytest.mark.parametrize("tag", ["v2.0.0", "app@v3.1.4"])
    def test_roundtrip_shape(self, tag):
        # releasable path re-emits a normalized ``v<version>`` in the message.
        version, msg = _version_and_msg(_uc(releasable_name="app"), tag)
        assert msg == f"app: release v{version}"
