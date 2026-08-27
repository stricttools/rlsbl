"""``rlsbl rewrite go-module-path``: the sweep, the counts, and the refusals.

The command renames a Go module path across a repository. Three properties are
what make it safe to run, and each is pinned below:

* the sweep is **boundary-aware** -- a neighbouring module whose path merely
  begins with the same letters is never rewritten;
* the sweep is **line-anchored** -- only lines the tree-sitter parser reported
  an import spec on are touched, so a comment or a string literal that happens
  to contain the old path survives;
* **preview and apply agree on counts** -- an apply that finds a different
  number of occurrences than the preview reported refuses instead of writing
  content nobody previewed.
"""

import os

import pytest

from rlsbl.commands.rewrite.go_module_path import (
    GoModuleRewriteError,
    apply_item,
    cmd_go_module_path,
    find_go_mod_files,
    observe,
    rewrite_go_mod_text,
    validate_module_paths,
)

OLD = "github.com/o/foo"
NEW = "github.com/n/qux"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def repo(tmp_path):
    """A small Go module that imports itself, a neighbour, and a third party."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "go.mod", (
        f"module {OLD}\n"
        "\n"
        "go 1.22\n"
        "\n"
        "require (\n"
        "\tgithub.com/o/foobar v1.0.0\n"
        "\tgithub.com/other/lib v0.3.0\n"
        ")\n"
    ))
    _write(root, "main.go", (
        "package main\n"
        "\n"
        "import (\n"
        f'\t"{OLD}/internal/svc"\n'
        '\t"github.com/o/foobar/pkg"\n'
        '\t"fmt"\n'
        ")\n"
        "\n"
        "func main() { fmt.Println(svc.X) }\n"
    ))
    _write(root, "internal/svc/svc.go", (
        "package svc\n"
        "\n"
        f'import "{OLD}/internal/deep"\n'
        "\n"
        "var X = deep.Y\n"
    ))
    _write(root, "internal/deep/deep.go", "package deep\n\nvar Y = 1\n")
    return root


class TestValidation:
    def test_identical_paths_refuse(self):
        with pytest.raises(GoModuleRewriteError, match="same path"):
            validate_module_paths(OLD, OLD)

    def test_empty_path_refuses(self):
        with pytest.raises(GoModuleRewriteError, match="--from-module"):
            validate_module_paths("", NEW)

    def test_whitespace_refuses(self):
        with pytest.raises(GoModuleRewriteError, match="whitespace"):
            validate_module_paths(OLD, "github.com/n/q ux")

    def test_a_module_nothing_references_refuses(self, repo):
        """A typo'd --from-module is a hard error, never a silent no-op."""
        with pytest.raises(GoModuleRewriteError, match="typo"):
            observe(repo, "github.com/someone/else", NEW)


class TestGoModRewrite:
    def test_module_directive_and_boundary(self):
        text = (
            f"module {OLD}\n"
            "\n"
            "require (\n"
            "\tgithub.com/o/foobar v1.0.0\n"
            f"\t{OLD}/sub v0.2.0\n"
            ")\n"
        )
        new_text, count, sites = rewrite_go_mod_text(text, OLD, NEW)
        assert count == 2, sites
        assert f"module {NEW}\n" in new_text
        assert f"\t{NEW}/sub v0.2.0\n" in new_text
        # The similarly-named neighbour survives verbatim.
        assert "github.com/o/foobar v1.0.0" in new_text

    def test_comments_are_left_alone(self):
        text = f"module {OLD} // was {OLD}\n"
        new_text, count, _ = rewrite_go_mod_text(text, OLD, NEW)
        assert count == 1
        assert new_text == f"module {NEW} // was {OLD}\n"

    def test_replace_directive_is_rewritten(self):
        text = f"replace {OLD}/sub => ./sub\n"
        new_text, count, _ = rewrite_go_mod_text(text, OLD, NEW)
        assert count == 1
        assert new_text == f"replace {NEW}/sub => ./sub\n"


class TestObserveAndApply:
    def test_preview_counts_every_file(self, repo):
        preview = observe(repo, OLD, NEW)
        counts = {
            item.key: item.data.occurrences
            for item in preview.items if item.data is not None
        }
        assert counts == {
            "go.mod": 1,
            "main.go": 1,
            os.path.join("internal", "svc", "svc.go"): 1,
        }
        # A trailing summary item states the whole sweep.
        assert preview.items[-1].key == "(total)"
        assert "3 occurrences across 3 files" in preview.items[-1].summary

    def test_apply_rewrites_and_leaves_the_neighbour_alone(self, repo):
        preview = observe(repo, OLD, NEW)
        for item in preview.items:
            apply_item(item, OLD, NEW)

        assert (repo / "go.mod").read_text().startswith(f"module {NEW}\n")
        assert "github.com/o/foobar v1.0.0" in (repo / "go.mod").read_text()

        main = (repo / "main.go").read_text()
        assert f'"{NEW}/internal/svc"' in main
        assert '"github.com/o/foobar/pkg"' in main
        # The only remaining mention of the old letters is the NEIGHBOUR's
        # path, which merely starts the same way.
        assert f'"{OLD}"' not in main
        assert f'"{OLD}/' not in main

        svc = (repo / "internal" / "svc" / "svc.go").read_text()
        assert f'import "{NEW}/internal/deep"' in svc

    def test_a_string_literal_outside_an_import_is_not_touched(self, repo, tmp_path):
        """Line-anchoring, demonstrated: only import specs are rewritten."""
        _write(repo, "doc.go", (
            "package main\n"
            "\n"
            f"// See {OLD}/docs for details.\n"
            f'const Home = "{OLD}/home"\n'
        ))
        preview = observe(repo, OLD, NEW)
        assert "doc.go" not in preview.keys
        for item in preview.items:
            apply_item(item, OLD, NEW)
        assert (repo / "doc.go").read_text().count(OLD) == 2

    def test_ordinary_go_directories_named_like_build_output_are_swept(self, repo):
        """``build``, ``assets`` and ``static`` are legal Go package names.

        The walk used to prune a linter's build-output exclusion list, so a
        package living in ``internal/assets`` was neither counted nor rewritten
        and the command still reported success -- leaving a tree that does not
        compile.  Everything except ``vendor/`` and ``.git/`` is swept.
        """
        _write(repo, "internal/assets/assets.go", (
            "package assets\n"
            "\n"
            f'import "{OLD}/internal/deep"\n'
        ))
        _write(repo, "cmd/build/main.go", (
            "package main\n"
            "\n"
            f'import "{OLD}/internal/svc"\n'
            "\n"
            "func main() {}\n"
        ))
        _write(repo, "web/static/gen.go", (
            "package static\n"
            "\n"
            f'import "{OLD}/internal/deep"\n'
        ))
        _write(repo, "node_modules/pkg/thing.go", (
            "package pkg\n"
            "\n"
            f'import "{OLD}/internal/deep"\n'
        ))
        _write(repo, "dist/go.mod", f"module {OLD}/dist\n")

        preview = observe(repo, OLD, NEW)
        for rel in (
            os.path.join("internal", "assets", "assets.go"),
            os.path.join("cmd", "build", "main.go"),
            os.path.join("web", "static", "gen.go"),
            os.path.join("node_modules", "pkg", "thing.go"),
            os.path.join("dist", "go.mod"),
        ):
            assert rel in preview.keys, f"{rel} missing from the plan"

        for item in preview.items:
            apply_item(item, OLD, NEW)
        assert NEW in (repo / "internal" / "assets" / "assets.go").read_text()
        assert NEW in (repo / "cmd" / "build" / "main.go").read_text()
        assert NEW in (repo / "web" / "static" / "gen.go").read_text()
        assert OLD not in (repo / "web" / "static" / "gen.go").read_text()

    def test_the_git_directory_is_never_swept(self, repo):
        """``.git`` is the repository's own storage, not source to rename."""
        _write(repo, ".git/hooks/thing.go", (
            "package hooks\n"
            "\n"
            f'import "{OLD}/internal/deep"\n'
        ))
        preview = observe(repo, OLD, NEW)
        assert not any(key.startswith(".git") for key in preview.keys)

    def test_vendored_trees_are_skipped(self, repo):
        _write(repo, "vendor/github.com/o/foo/v.go", (
            "package foo\n"
            "\n"
            f'import "{OLD}/internal/deep"\n'
        ))
        _write(repo, "vendor/github.com/o/foo/go.mod", f"module {OLD}\n")
        rels = [os.path.relpath(p, repo) for p in find_go_mod_files(repo)]
        assert rels == ["go.mod"]
        preview = observe(repo, OLD, NEW)
        assert not any(key.startswith("vendor") for key in preview.keys)

    def test_rerunning_after_a_full_apply_finds_nothing(self, repo):
        for item in observe(repo, OLD, NEW).items:
            apply_item(item, OLD, NEW)
        # Nothing references the old path any more, so a re-run refuses.
        with pytest.raises(GoModuleRewriteError, match="nothing references"):
            observe(repo, OLD, NEW)
        # And the reverse rename sweeps exactly the same three files back.
        preview = observe(repo, NEW, OLD)
        assert [i.data.occurrences for i in preview.items if i.data] == [1, 1, 1]

    def test_a_consumer_repo_rewrites_references_without_owning_the_module(
        self, tmp_path
    ):
        """The module need not be declared here -- an upstream move is a sweep too."""
        root = tmp_path / "consumer"
        root.mkdir()
        _write(root, "go.mod", (
            "module example.com/app\n\nrequire " + OLD + " v1.0.0\n"
        ))
        _write(root, "use.go", (
            "package app\n\n" + f'import "{OLD}/pkg"\n'
        ))
        preview = observe(root, OLD, NEW)
        assert [i.key for i in preview.items] == ["go.mod", "use.go", "(total)"]
        assert "CONSUMES the module" in preview.items[-1].facts[0]
        for item in preview.items:
            apply_item(item, OLD, NEW)
        assert f"require {NEW} v1.0.0" in (root / "go.mod").read_text()
        assert f'"{NEW}/pkg"' in (root / "use.go").read_text()


class TestTheCountContract:
    def test_a_count_that_moved_aborts_without_writing(self, repo):
        preview = observe(repo, OLD, NEW)
        target = preview.by_key("main.go")

        # The tree moves underneath the plan: a second import appears.
        (repo / "main.go").write_text(
            "package main\n"
            "\n"
            "import (\n"
            f'\t"{OLD}/internal/svc"\n'
            f'\t"{OLD}/internal/deep"\n'
            ")\n"
            "\n"
            "func main() {}\n"
        )
        before = (repo / "main.go").read_text()

        with pytest.raises(GoModuleRewriteError, match="counted 1"):
            apply_item(target, OLD, NEW)
        assert (repo / "main.go").read_text() == before

    def test_the_abort_names_the_remedy(self, repo):
        preview = observe(repo, OLD, NEW)
        target = preview.by_key("go.mod")
        (repo / "go.mod").write_text("module example.com/moved\n")
        with pytest.raises(GoModuleRewriteError) as exc:
            apply_item(target, OLD, NEW)
        assert "--dry-run" in str(exc.value)
        assert "nothing further has been written" in str(exc.value)


class TestASingleOccurrenceRepo:
    def test_a_module_directive_alone_is_a_one_file_plan(self, tmp_path):
        root = tmp_path / "bare"
        root.mkdir()
        _write(root, "go.mod", f"module {OLD}\n")
        preview = observe(root, OLD, NEW)
        assert preview.keys == ("go.mod", "(total)")
        assert "1 occurrence across 1 file" in preview.items[-1].summary


class TestRawStringImports:
    """Go's second string form is a legal import literal, and gets renamed.

    ``import `example.com/o/foo` `` compiles exactly like the double-quoted
    spelling.  The tree-sitter scanner used to collect only
    ``interpreted_string_literal``, so a raw-string import was invisible: not
    counted, not rewritten, not reported.
    """

    def test_the_scanner_sees_both_quote_forms(self, tmp_path):
        from rlsbl.lint.go_ast import scan_imports

        src = _write(tmp_path, "both.go", (
            "package main\n"
            "\n"
            "import (\n"
            f"\t`{OLD}/internal/raw`\n"
            f'\t"{OLD}/internal/interp"\n'
            ")\n"
        ))
        found = scan_imports(str(src))
        assert [(p, ln) for p, _fp, ln in found] == [
            (f"{OLD}/internal/raw", 4),
            (f"{OLD}/internal/interp", 5),
        ]

    def test_a_single_raw_import_is_scanned(self, tmp_path):
        from rlsbl.lint.go_ast import scan_imports

        src = _write(tmp_path, "solo.go", (
            "package main\n"
            "\n"
            f"import `{OLD}/pkg`\n"
        ))
        assert [p for p, _fp, _ln in scan_imports(str(src))] == [f"{OLD}/pkg"]

    def test_a_raw_import_is_planned_and_rewritten(self, repo):
        _write(repo, "raw.go", (
            "package main\n"
            "\n"
            "import (\n"
            f"\t`{OLD}/internal/deep`\n"
            f'\t"{OLD}/internal/svc"\n'
            ")\n"
        ))
        preview = observe(repo, OLD, NEW)
        assert preview.by_key("raw.go").data.occurrences == 2
        for item in preview.items:
            apply_item(item, OLD, NEW)
        text = (repo / "raw.go").read_text()
        assert f"`{NEW}/internal/deep`" in text
        assert f'"{NEW}/internal/svc"' in text
        assert OLD not in text

    def test_a_raw_and_an_interpreted_import_share_one_line(self, repo):
        """Both literals on one line are rewritten, each in its own form."""
        _write(repo, "oneline.go", (
            "package main\n"
            "\n"
            f'import (`{OLD}/internal/deep`; "{OLD}/internal/svc")\n'
        ))
        preview = observe(repo, OLD, NEW)
        assert preview.by_key("oneline.go").data.occurrences == 2
        for item in preview.items:
            apply_item(item, OLD, NEW)
        text = (repo / "oneline.go").read_text()
        assert f"`{NEW}/internal/deep`" in text
        assert f'"{NEW}/internal/svc"' in text


class TestTheWalkerParameter:
    """``walk_source_files`` prunes the caller's names, not a fixed list."""

    def test_the_default_is_the_linter_set(self, tmp_path):
        from rlsbl.lint.utils import LINTER_EXCLUDED_DIRS, walk_source_files

        for rel in ("build/a.go", "assets/b.go", "pkg.egg-info/c.go", "src/d.go"):
            _write(tmp_path, rel, "package p\n")
        found = walk_source_files(str(tmp_path), (".go",), [])
        assert [os.path.relpath(p, tmp_path) for p in found] == [
            os.path.join("src", "d.go")
        ]
        assert "build" in LINTER_EXCLUDED_DIRS
        assert "*.egg-info" in LINTER_EXCLUDED_DIRS

    def test_an_explicit_set_replaces_it_entirely(self, tmp_path):
        from rlsbl.lint.utils import walk_source_files

        for rel in ("build/a.go", "vendor/b.go", "src/d.go"):
            _write(tmp_path, rel, "package p\n")
        found = walk_source_files(
            str(tmp_path), (".go",), [], excluded_dir_names=frozenset({"vendor"}),
        )
        assert sorted(os.path.relpath(p, tmp_path) for p in found) == [
            os.path.join("build", "a.go"),
            os.path.join("src", "d.go"),
        ]


class TestCommandEntryPoint:
    def test_dry_run_prints_the_plan_and_writes_nothing(self, repo, capsys):
        before = {
            p: (repo / p).read_text()
            for p in ("go.mod", "main.go")
        }
        cmd_go_module_path(
            {"from-module": OLD, "to-module": NEW, "dry-run": True},
            project_root=repo,
        )
        out = capsys.readouterr().out
        assert "go.mod: rewrite" in out
        assert "main.go: rewrite" in out
        assert f"{OLD} -> {NEW}" in out
        for path, text in before.items():
            assert (repo / path).read_text() == text

    def test_apply_writes(self, repo, capsys):
        cmd_go_module_path(
            {"from-module": OLD, "to-module": NEW, "dry-run": False},
            project_root=repo,
        )
        out = capsys.readouterr().out
        assert "Renamed" in out
        assert (repo / "go.mod").read_text().startswith(f"module {NEW}")

    def test_bad_input_exits_one(self, repo, capsys):
        with pytest.raises(SystemExit) as exc:
            cmd_go_module_path(
                {"from-module": OLD, "to-module": OLD, "dry-run": True},
                project_root=repo,
            )
        assert exc.value.code == 1
        assert "same path" in capsys.readouterr().err
