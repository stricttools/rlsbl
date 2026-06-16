"""Tests for the assembly push block in the post-release hook template."""

from __future__ import annotations

import os

TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates"
)

POST_RELEASE_TPL = os.path.join(
    TEMPLATES_ROOT, "shared", "hooks", "post-release.sh.tpl"
)


def _read_template() -> str:
    with open(POST_RELEASE_TPL) as f:
        return f.read()


class TestPostReleaseAssemblyBlock:
    """Verify the post-release hook template includes the assembly push block."""

    def test_template_contains_selfdoc_assembly_push(self):
        content = _read_template()
        assert "selfdoc assembly push" in content

    def test_assembly_push_is_nonfatal(self):
        """The assembly push must not abort the hook on failure."""
        content = _read_template()
        # The command should be guarded with || so failures are non-fatal
        assert 'selfdoc assembly push || echo "Warning: assembly push failed (non-fatal)"' in content

    def test_assembly_push_gated_on_selfdoc_json(self):
        """The block must check for selfdoc.json before attempting push."""
        content = _read_template()
        assert "[ -f selfdoc.json ]" in content

    def test_assembly_push_gated_on_selfdoc_command(self):
        """The block must check that selfdoc is installed."""
        content = _read_template()
        assert "command -v selfdoc" in content

    def test_assembly_push_checks_assembly_config(self):
        """The block must verify assembly config exists in selfdoc.json."""
        content = _read_template()
        # Should check for assembly key or topology.assembly
        assert "c.get('assembly')" in content
        assert "c.get('topology')" in content

    def test_assembly_push_comes_after_post_release_echo(self):
        """The assembly block must appear after the initial post-release echo."""
        content = _read_template()
        echo_pos = content.index('echo "Post-release: v$RLSBL_VERSION"')
        push_pos = content.index("selfdoc assembly push")
        assert push_pos > echo_pos
