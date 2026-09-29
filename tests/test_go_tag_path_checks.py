"""Two Go tag rules checked before anything is tagged.

A Go tag is the published artifact: the module proxy caches the version a
tag names permanently. So a releasable whose ``tag_format`` is a Go path tag
(``web/v{version}``) must name the path of one of its own Go members -- any
other path publishes a version of a module the releasable does not own, or of
none -- and a Go module whose path ends in a major-version suffix (``/v2``)
cannot be published by the ``<path>/v{version}`` tags rlsbl derives.
"""

import shlex

import pytest

from conftest import make_nested_workspace, run_git


def _ctx(root):
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_releasables, load_workspace

    projects = load_workspace(str(root))
    return WorkspaceCheckContext(
        project_root=root, workspace_root=root, config={},
        projects=projects, releasables=load_releasables(str(root), projects),
    )


def _run(root, name):
    from rlsbl import app

    return app._check_defs[name].impl(_ctx(root))


def _text(result):
    return " ".join(p.text for p in result.problems)


def _set_tag_format(root, releasable, fmt):
    toml = root / ".rlsbl-monorepo" / "workspace.toml"
    text = toml.read_text()
    lines = text.splitlines()
    out = []
    in_rel = False
    for line in lines:
        if line.strip() == "[[releasables]]":
            in_rel = False
        if line.strip() == f'name = "{releasable}"':
            in_rel = True
        if in_rel and line.startswith("tag_format"):
            line = f'tag_format = "{fmt}"'
            in_rel = False
        out.append(line)
    toml.write_text("\n".join(out) + "\n")


# ---------------------------------------------------------------------------
# path-tag-format-go-member
# ---------------------------------------------------------------------------


class TestPathTagFormatGoMember:
    def test_the_derived_path_formats_pass(self, tmp_path):
        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        assert not _run(root, "path-tag-format-go-member").problems

    def test_a_shared_releasable_tagged_at_one_members_path_passes(self, tmp_path):
        root = tmp_path / "ws"
        make_nested_workspace(root, "shared")
        assert not _run(root, "path-tag-format-go-member").problems

    def test_a_path_naming_no_go_member_is_refused(self, tmp_path):
        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        _set_tag_format(root, "drawcmd", "draw/cli/v{version}")
        text = _text(_run(root, "path-tag-format-go-member"))
        assert "releasable 'drawcmd' declares tag_format 'draw/cli/v{version}'" in text
        assert "no Go member of 'drawcmd' lives at 'draw/cli'" in text
        assert "'{name}@v{version}'" in text
        assert "draw/cmd" in text

    def test_a_path_naming_another_releasables_module_is_refused(self, tmp_path):
        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        _set_tag_format(root, "drawcmd", "draw/v{version}")
        text = _text(_run(root, "path-tag-format-go-member"))
        assert "no Go member of 'drawcmd' lives at 'draw'" in text

    @pytest.mark.parametrize("fix", ["draw/cmd/v{version}", "{name}@v{version}"])
    def test_either_named_fix_clears_the_refusal(self, tmp_path, fix):
        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        _set_tag_format(root, "drawcmd", "draw/cli/v{version}")
        assert _run(root, "path-tag-format-go-member").problems
        _set_tag_format(root, "drawcmd", fix)
        assert not _run(root, "path-tag-format-go-member").problems

    def test_a_non_path_format_is_not_this_checks_business(self, tmp_path):
        root = tmp_path / "ws"
        make_nested_workspace(root, "python")
        result = _run(root, "path-tag-format-go-member")
        assert not result.problems


# ---------------------------------------------------------------------------
# go-module-major-suffix
# ---------------------------------------------------------------------------


def _declare_module(root, member, module):
    go_mod = root / member / "go.mod"
    lines = go_mod.read_text().splitlines()
    lines[0] = f"module {module}"
    go_mod.write_text("\n".join(lines) + "\n")


class TestGoModuleMajorSuffix:
    PREFIX = "github.com/example/nested"

    def test_plain_module_paths_pass(self, tmp_path):
        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        assert not _run(root, "go-module-major-suffix").problems

    def test_a_major_version_suffix_is_refused(self, tmp_path):
        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        _declare_module(root, "kernel", f"{self.PREFIX}/kernel/v2")
        text = _text(_run(root, "go-module-major-suffix"))
        assert f"'{self.PREFIX}/kernel/v2'" in text
        assert "/v2" in text
        assert (
            f"rlsbl rewrite go-module-path --from-module {self.PREFIX}/kernel/v2 "
            f"--to-module {self.PREFIX}/kernel"
        ) in text

    def test_a_v_directory_name_alone_is_not_a_suffix(self, tmp_path):
        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        # kernel/vulkan: the last element starts with "v" but is no major.
        assert not _run(root, "go-module-major-suffix").problems

    def test_running_the_named_rewrite_clears_the_refusal(self, tmp_path, monkeypatch):
        import rlsbl

        root = tmp_path / "ws"
        make_nested_workspace(root, "go")
        _declare_module(root, "kernel", f"{self.PREFIX}/kernel/v2")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-q", "-m", "a v2 module path")
        text = _text(_run(root, "go-module-major-suffix"))
        command = text[text.index("rlsbl rewrite go-module-path"):].split("`")[0]
        monkeypatch.chdir(root)
        result = rlsbl.app.test(shlex.split(command)[1:])
        assert result.exit_code == 0, result.stderr
        assert not _run(root, "go-module-major-suffix").problems


def test_a_release_preflight_context_listing_one_member_is_judged_whole(tmp_path):
    """The release hands each member a context listing that member alone."""
    from rlsbl import app
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_releasables, load_workspace

    root = tmp_path / "ws"
    make_nested_workspace(root, "shared")
    projects = load_workspace(str(root))
    shader = next(p for p in projects if p["name"] == "shader")
    ctx = WorkspaceCheckContext(
        project_root=root / "gfx" / "shader", workspace_root=root, config={},
        projects=[shader], releasables=load_releasables(str(root), projects),
    )
    assert not app._check_defs["path-tag-format-go-member"].impl(ctx).problems
