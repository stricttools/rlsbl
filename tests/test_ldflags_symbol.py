"""Tests for the ``ldflags-symbol`` check.

The defect class: ``go build -ldflags "-X importpath.Symbol=value"`` overwrites
a package-level string variable at link time, and a ``-X`` flag naming a symbol
that does not exist links SILENTLY -- no error, no warning, nothing set. A
build configuration that says ``-X main.Version=...`` while the Go source
declares ``var version`` therefore ships binaries that report their fallback
version forever, and nothing in the toolchain says so.

The second route to the identical user-visible bug: the symbol exists and
receives the value, but nothing reads it -- the version surface prints a
hardcoded literal instead. That one warns rather than errors, because fixing it
can mean adding a version surface rather than renaming a variable.
"""

import json
import os

import pytest

from rlsbl import app
from rlsbl.checks import CHECK_TARGETS
from rlsbl.ldflags_symbols import evaluate_ldflags_symbols

from conftest import make_ctx
from githarness import git, init_repo


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Fixture material
# ---------------------------------------------------------------------------


def _goreleaser(target, main="."):
    """A minimal goreleaser document injecting *target* into the *main* build."""
    return (
        "version: 2\n"
        "\n"
        "builds:\n"
        f"  - main: {main}\n"
        "    ldflags:\n"
        f"      - -s -w -X {target}={{{{.Version}}}}\n"
    )


#: A version.go that declares the symbol AND reads it, the shape rlsbl's own
#: Go scaffold emits.
def _version_go(symbol, package="main"):
    return (
        f"package {package}\n"
        "\n"
        'import "strings"\n'
        "\n"
        f"var {symbol} string\n"
        "\n"
        "func init() {\n"
        f"\tif {symbol} == \"\" {{\n"
        f"\t\t{symbol} = strings.TrimPrefix(\"v0.0.0\", \"v\")\n"
        "\t}\n"
        "}\n"
    )


def _main_go(body='\tprintln("hi")\n', package="main"):
    return f"package {package}\n\nfunc main() {{\n{body}}}\n"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _go_project(root, files, *, module="github.com/owner/repo", config=None):
    """A committed Go project with *files* written relative to its root."""
    root.mkdir(parents=True, exist_ok=True)
    init_repo(root)
    _write(
        root, ".rlsbl/config.json",
        json.dumps(config or {"publish_mode": "ci", "targets": ["go"]}),
    )
    _write(root, "go.mod", f"module {module}\n\ngo 1.23\n")
    _write(root, "VERSION", "0.1.0\n")
    for rel, text in files.items():
        _write(root, rel, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture")
    return root


def _walk(directory):
    """Every file under *directory*, as the tracked-file enumerator's stand-in."""
    out = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), directory)
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _evaluate(directory):
    return evaluate_ldflags_symbols([str(directory)], list_tracked=_walk)


def _run(root):
    ctx = make_ctx(root)
    return app._check_defs["ldflags-symbol"].impl(ctx)


def _text(result):
    return " ".join(p.text for p in result.problems)


# ---------------------------------------------------------------------------
# The headline defect, through the registered check
# ---------------------------------------------------------------------------


class TestCapitalizationMismatch:
    """`-X main.Version` against `var version`: five fleet projects shipped it."""

    FILES = {
        ".goreleaser.yml": _goreleaser("main.Version"),
        "version.go": _version_go("version"),
        "main.go": _main_go('\tprintln(version)\n'),
    }

    def test_the_mismatch_is_an_error(self, tmp_path):
        root = _go_project(tmp_path / "repo", dict(self.FILES))
        result = _run(root)
        assert result.status == "fail", result
        text = _text(result)
        assert ".goreleaser.yml:6" in text, text
        assert "main.Version" in text
        assert "version" in text

    def test_the_error_names_both_the_symbol_and_the_directory(self, tmp_path):
        root = _go_project(tmp_path / "repo", dict(self.FILES))
        text = _text(_run(root))
        # The symbol the linker names, the directory searched for it, and the
        # two-way remedy.
        assert "Version" in text
        assert "." in text
        assert "Rename" in text or "rename" in text
        assert "-X" in text

    def test_the_error_says_the_flag_sets_nothing(self, tmp_path):
        """The reason this is an error and not a style note."""
        root = _go_project(tmp_path / "repo", dict(self.FILES))
        text = _text(_run(root))
        assert "silent" in text.lower()

    def test_performing_the_prescribed_remedy_clears_the_finding(self, tmp_path):
        """Remedy truthfulness: the error names a fix, so the fix is executed.

        The message prescribes renaming the Go variable to the name the linker
        targets. This test performs exactly that and asserts the check passes
        afterwards -- an error naming a remedy nobody ever ran is worthless.
        """
        root = _go_project(tmp_path / "repo", dict(self.FILES))
        assert _run(root).status == "fail"

        # The remedy, verbatim: rename the Go variable to `Version`.
        _write(root, "version.go", _version_go("Version"))
        _write(root, "main.go", _main_go('\tprintln(Version)\n'))
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "rename the injected symbol")

        after = _run(root)
        assert after.status == "pass", _text(after)

    def test_the_other_prescribed_remedy_also_clears_the_finding(self, tmp_path):
        """The message's second branch: change the -X target instead."""
        root = _go_project(tmp_path / "repo", dict(self.FILES))
        assert _run(root).status == "fail"

        _write(root, ".goreleaser.yml", _goreleaser("main.version"))
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "retarget the -X flag")

        after = _run(root)
        assert after.status == "pass", _text(after)


# ---------------------------------------------------------------------------
# Symbol-agnostic: the relationship is checked, never a naming convention
# ---------------------------------------------------------------------------


class TestSymbolAgnostic:
    def test_agreeing_lowercase_names_pass(self, tmp_path):
        """`-X main.version` + `var version` is correct and must not churn."""
        root = _go_project(tmp_path / "repo", {
            ".goreleaser.yml": _goreleaser("main.version"),
            "version.go": _version_go("version"),
            "main.go": _main_go('\tprintln(version)\n'),
        })
        result = _run(root)
        assert result.status == "pass", _text(result)

    def test_agreeing_capitalized_names_pass(self, tmp_path):
        root = _go_project(tmp_path / "repo", {
            ".goreleaser.yml": _goreleaser("main.Version"),
            "version.go": _version_go("Version"),
            "main.go": _main_go('\tprintln(Version)\n'),
        })
        result = _run(root)
        assert result.status == "pass", _text(result)

    def test_an_arbitrary_agreeing_name_passes(self, tmp_path):
        root = _go_project(tmp_path / "repo", {
            ".goreleaser.yml": _goreleaser("main.buildTag"),
            "version.go": _version_go("buildTag"),
            "main.go": _main_go('\tprintln(buildTag)\n'),
        })
        result = _run(root)
        assert result.status == "pass", _text(result)


# ---------------------------------------------------------------------------
# The second route to the same bug: injected, then never read
# ---------------------------------------------------------------------------


class TestUnreadSymbol:
    FILES = {
        ".goreleaser.yml": _goreleaser("main.Version"),
        "version.go": "package main\n\nvar Version string\n",
        "main.go": _main_go('\tprintln("0.1.0")\n'),
    }

    def test_an_unread_symbol_warns_and_does_not_error(self, tmp_path):
        root = _go_project(tmp_path / "repo", dict(self.FILES))
        result = _run(root)
        assert result.status == "warn", _text(result)
        assert [p.severity for p in result.problems] == ["warn"]
        text = _text(result)
        assert "main.Version" in text
        assert "reads" in text

    def test_reading_the_symbol_clears_the_warning(self, tmp_path):
        root = _go_project(tmp_path / "repo", dict(self.FILES))
        assert _run(root).status == "warn"

        _write(root, "main.go", _main_go('\tprintln(Version)\n'))
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "print the injected version")

        after = _run(root)
        assert after.status == "pass", _text(after)

    def test_a_reader_in_another_package_counts(self, tmp_path):
        """A `-X` into a library package is read from the binary, not beside it."""
        root = _go_project(tmp_path / "repo", {
            ".goreleaser.yml": _goreleaser(
                "github.com/owner/repo/internal/version.Value", main="./cmd/tool",
            ),
            "internal/version/version.go": "package version\n\nvar Value string\n",
            "cmd/tool/main.go": (
                "package main\n"
                "\n"
                'import "github.com/owner/repo/internal/version"\n'
                "\n"
                "func main() {\n"
                "\tprintln(version.Value)\n"
                "}\n"
            ),
        })
        result = _run(root)
        assert result.status == "pass", _text(result)


# ---------------------------------------------------------------------------
# Nothing to verify is a pass, never a finding
# ---------------------------------------------------------------------------


class TestNothingInjected:
    def test_a_project_with_no_x_flag_passes(self, tmp_path):
        """A project may embed its version with `go:embed` and inject nothing."""
        root = _go_project(tmp_path / "repo", {
            ".goreleaser.yml": (
                "version: 2\n\nbuilds:\n  - main: .\n    ldflags:\n      - -s -w\n"
            ),
            "main.go": _main_go(),
        })
        result = _run(root)
        assert result.status == "pass", _text(result)

    def test_a_project_with_no_build_configuration_at_all_passes(self, tmp_path):
        root = _go_project(tmp_path / "repo", {"main.go": _main_go()})
        result = _run(root)
        assert result.status == "pass", _text(result)

    def test_a_project_with_no_go_target_skips(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        _write(root, ".rlsbl/config.json",
               json.dumps({"publish_mode": "ci", "targets": ["pypi"]}))
        _write(root, "pyproject.toml",
               '[project]\nname = "p"\nversion = "0.1.0"\n')
        result = _run(root)
        assert result.status == "skip"


# ---------------------------------------------------------------------------
# Which files are scanned
# ---------------------------------------------------------------------------


class TestScannedFiles:
    @pytest.mark.parametrize("rel", [
        ".goreleaser.yml",
        ".goreleaser.yaml",
    ])
    def test_both_goreleaser_spellings_are_read(self, tmp_path, rel):
        root = tmp_path / rel.replace(".", "_")
        _go_project(root, {
            rel: _goreleaser("main.Version"),
            "version.go": _version_go("version"),
            "main.go": _main_go('\tprintln(version)\n'),
        })
        assert _run(root).status == "fail"

    def test_a_makefile_is_read(self, tmp_path):
        root = _go_project(tmp_path / "repo", {
            "Makefile": (
                "build:\n"
                "\tgo build -ldflags \"-X main.Version=$(VERSION)\" -o bin/tool .\n"
            ),
            "version.go": _version_go("version"),
            "main.go": _main_go('\tprintln(version)\n'),
        })
        result = _run(root)
        assert result.status == "fail"
        assert "Makefile:2" in _text(result)

    def test_a_shell_script_is_read(self, tmp_path):
        root = _go_project(tmp_path / "repo", {
            "scripts/build.sh": (
                "#!/usr/bin/env bash\n"
                "go build -ldflags \"-X main.Version=${VERSION}\" .\n"
            ),
            "version.go": _version_go("version"),
            "main.go": _main_go('\tprintln(version)\n'),
        })
        result = _run(root)
        assert result.status == "fail"
        assert "scripts/build.sh:2" in _text(result)

    def test_a_ci_workflow_is_read(self, tmp_path):
        root = _go_project(tmp_path / "repo", {
            ".github/workflows/ci.yml": (
                "name: CI\non: [push]\njobs:\n"
                "  build:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - run: go build -ldflags \"-X main.Version=x\" .\n"
            ),
            "version.go": _version_go("version"),
            "main.go": _main_go('\tprintln(version)\n'),
        })
        result = _run(root)
        assert result.status == "fail"
        assert ".github/workflows/ci.yml:7" in _text(result)

    def test_scaffold_base_copies_are_skipped(self, tmp_path):
        """`.rlsbl/bases/` holds scaffold base copies, not live configuration."""
        root = _go_project(tmp_path / "repo", {
            ".rlsbl/bases/.goreleaser.yml": _goreleaser("main.Version"),
            "version.go": _version_go("version"),
            "main.go": _main_go('\tprintln(version)\n'),
        })
        result = _run(root)
        assert result.status == "pass", _text(result)

    def test_goreleaser_output_is_skipped(self, tmp_path):
        root = _go_project(tmp_path / "repo", {
            "dist/config.yaml": _goreleaser("main.Version"),
            "version.go": _version_go("version"),
            "main.go": _main_go('\tprintln(version)\n'),
        })
        result = _run(root)
        assert result.status == "pass", _text(result)

    def test_an_untracked_build_file_is_not_read(self, tmp_path):
        """Only what git tracks is this project's build configuration."""
        root = _go_project(tmp_path / "repo", {
            ".goreleaser.yml": _goreleaser("main.version"),
            "version.go": _version_go("version"),
            "main.go": _main_go('\tprintln(version)\n'),
        })
        _write(root, "Makefile", "build:\n\tgo build -ldflags \"-X main.Nope=x\" .\n")
        result = _run(root)
        assert result.status == "pass", _text(result)


# ---------------------------------------------------------------------------
# What the linker can and cannot set
# ---------------------------------------------------------------------------


class TestDeclarationShapes:
    def _project(self, tmp_path, declaration, *, target="main.Version"):
        root = tmp_path / "repo"
        root.mkdir(parents=True, exist_ok=True)
        _write(root, "go.mod", "module github.com/owner/repo\n\ngo 1.23\n")
        _write(root, ".goreleaser.yml", _goreleaser(target))
        _write(root, "version.go", declaration)
        _write(root, "main.go", _main_go('\tprintln(Version)\n'))
        return root

    def test_an_uninitialized_string_var_is_injectable(self, tmp_path):
        root = self._project(tmp_path, "package main\n\nvar Version string\n")
        verdict = _evaluate(root)
        assert verdict.ok, verdict.problems

    def test_a_string_var_initialized_to_a_literal_is_injectable(self, tmp_path):
        root = self._project(
            tmp_path, 'package main\n\nvar Version string = "dev"\n',
        )
        verdict = _evaluate(root)
        assert verdict.ok, verdict.problems

    def test_an_inferred_string_var_is_injectable(self, tmp_path):
        root = self._project(tmp_path, 'package main\n\nvar Version = "dev"\n')
        verdict = _evaluate(root)
        assert verdict.ok, verdict.problems

    def test_a_grouped_var_block_is_read(self, tmp_path):
        root = self._project(
            tmp_path,
            'package main\n\nvar (\n\tName    = "tool"\n\tVersion string\n)\n',
        )
        verdict = _evaluate(root)
        assert verdict.ok, verdict.problems

    def test_a_const_is_an_error(self, tmp_path):
        root = self._project(
            tmp_path, 'package main\n\nconst Version = "0.1.0"\n',
        )
        verdict = _evaluate(root)
        assert not verdict.ok
        assert "const" in verdict.problems[0]

    def test_a_non_string_var_is_an_error(self, tmp_path):
        root = self._project(tmp_path, "package main\n\nvar Version int\n")
        verdict = _evaluate(root)
        assert not verdict.ok
        assert "int" in verdict.problems[0]

    def test_a_var_initialized_to_a_call_is_an_error(self, tmp_path):
        root = self._project(
            tmp_path,
            "package main\n\nimport \"os\"\n\nvar Version = os.Getenv(\"V\")\n",
        )
        verdict = _evaluate(root)
        assert not verdict.ok
        assert "constant" in verdict.problems[0]

    def test_a_function_of_that_name_is_an_error(self, tmp_path):
        root = self._project(
            tmp_path,
            'package main\n\nfunc Version() string { return "0.1.0" }\n',
        )
        verdict = _evaluate(root)
        assert not verdict.ok
        assert "function" in verdict.problems[0]

    def test_a_local_variable_does_not_satisfy_the_flag(self, tmp_path):
        """Only a PACKAGE-level var can be set by the linker."""
        root = self._project(
            tmp_path,
            'package main\n\nfunc helper() {\n\tVersion := "x"\n\t_ = Version\n}\n',
        )
        verdict = _evaluate(root)
        assert not verdict.ok
        assert "declares no" in verdict.problems[0]


# ---------------------------------------------------------------------------
# Where `-X <importpath>` points
# ---------------------------------------------------------------------------


class TestImportPathResolution:
    def test_goreleaser_main_names_the_package_for_a_bare_main(self, tmp_path):
        root = tmp_path / "repo"
        _write(root, "go.mod", "module github.com/owner/repo\n\ngo 1.23\n")
        _write(root, ".goreleaser.yml", _goreleaser("main.Version", main="./cmd/tool"))
        _write(root, "cmd/tool/version.go", "package main\n\nvar Version string\n")
        _write(root, "cmd/tool/main.go", _main_go('\tprintln(Version)\n'))
        verdict = _evaluate(root)
        assert verdict.ok, verdict.problems

    def test_the_wrong_directory_is_not_searched(self, tmp_path):
        """The symbol sits at the repo root; the build's main is ./cmd/tool."""
        root = tmp_path / "repo"
        _write(root, "go.mod", "module github.com/owner/repo\n\ngo 1.23\n")
        _write(root, ".goreleaser.yml", _goreleaser("main.Version", main="./cmd/tool"))
        _write(root, "version.go", "package main\n\nvar Version string\n")
        _write(root, "cmd/tool/main.go", _main_go())
        verdict = _evaluate(root)
        assert not verdict.ok
        assert "cmd/tool" in verdict.problems[0]

    def test_a_main_go_file_as_the_build_main_resolves_to_its_directory(self, tmp_path):
        root = tmp_path / "repo"
        _write(root, "go.mod", "module github.com/owner/repo\n\ngo 1.23\n")
        _write(
            root, ".goreleaser.yml",
            _goreleaser("main.Version", main="./cmd/tool/main.go"),
        )
        _write(root, "cmd/tool/main.go",
               "package main\n\nvar Version string\n\nfunc main() { println(Version) }\n")
        verdict = _evaluate(root)
        assert verdict.ok, verdict.problems

    def test_a_qualified_import_path_resolves_through_go_mod(self, tmp_path):
        root = tmp_path / "repo"
        _write(root, "go.mod", "module github.com/owner/repo\n\ngo 1.23\n")
        _write(root, "Makefile",
               "build:\n\tgo build -ldflags \"-X "
               "github.com/owner/repo/internal/build.Version=x\" ./cmd/tool\n")
        _write(root, "internal/build/build.go", "package build\n\nvar Version string\n")
        _write(root, "cmd/tool/main.go",
               "package main\n\nimport \"github.com/owner/repo/internal/build\"\n\n"
               "func main() { println(build.Version) }\n")
        verdict = _evaluate(root)
        assert verdict.ok, verdict.problems

    def test_a_qualified_path_with_no_such_symbol_is_an_error(self, tmp_path):
        root = tmp_path / "repo"
        _write(root, "go.mod", "module github.com/owner/repo\n\ngo 1.23\n")
        _write(root, "Makefile",
               "build:\n\tgo build -ldflags \"-X "
               "github.com/owner/repo/internal/build.Version=x\" ./cmd/tool\n")
        _write(root, "internal/build/build.go", "package build\n\nvar version string\n")
        _write(root, "cmd/tool/main.go", _main_go())
        verdict = _evaluate(root)
        assert not verdict.ok
        assert "internal/build" in verdict.problems[0]

    def test_a_makefile_target_argument_names_the_main_package(self, tmp_path):
        root = tmp_path / "repo"
        _write(root, "go.mod", "module github.com/owner/repo\n\ngo 1.23\n")
        _write(root, "Makefile",
               "build:\n\tgo build -ldflags \"-X main.Version=$(V)\" ./cmd/tool\n")
        _write(root, "cmd/tool/main.go",
               "package main\n\nvar Version string\n\nfunc main() { println(Version) }\n")
        verdict = _evaluate(root)
        assert verdict.ok, verdict.problems

    def test_a_package_outside_the_module_is_reported_as_unverified(self, tmp_path):
        """A third-party symbol cannot be resolved here, and is not guessed."""
        root = tmp_path / "repo"
        _write(root, "go.mod", "module github.com/owner/repo\n\ngo 1.23\n")
        _write(root, "Makefile",
               "build:\n\tgo build -ldflags \"-X "
               "github.com/other/dep/version.Value=x\" .\n")
        _write(root, "main.go", _main_go())
        verdict = _evaluate(root)
        assert verdict.ok, verdict.problems
        assert verdict.notes
        assert "github.com/other/dep/version" in " ".join(verdict.notes)

    def test_a_templated_import_path_is_reported_as_unverified(self, tmp_path):
        root = tmp_path / "repo"
        _write(root, "go.mod", "module github.com/owner/repo\n\ngo 1.23\n")
        _write(root, ".goreleaser.yml",
               _goreleaser("{{ .Env.MODULE }}/cmd/x.Version"))
        _write(root, "main.go", _main_go())
        verdict = _evaluate(root)
        assert verdict.ok, verdict.problems
        assert verdict.notes


# ---------------------------------------------------------------------------
# The scaffold's own pair of templates
# ---------------------------------------------------------------------------


class TestScaffoldTemplates:
    """The Go scaffold writes both halves of the coupling, so they must agree.

    ``templates/go/goreleaser.yml.tpl`` chooses the symbol name and
    ``templates/go/version.go.tpl`` declares it. A project scaffolded today
    inherits whatever those two say; if they ever disagree, every Go project
    rlsbl scaffolds acquires the defect this check exists to find.
    """

    def _render(self, name, substitutions):
        path = os.path.join(REPO_ROOT, "rlsbl", "templates", "go", name)
        text = open(path, encoding="utf-8").read()
        for placeholder, value in substitutions.items():
            text = text.replace(placeholder, value)
        return text

    def test_the_scaffolded_pair_passes_the_check(self, tmp_path):
        goreleaser = self._render("goreleaser.yml.tpl", {
            "{{goreleaserMain}}": ".",
            "{{brewsSection}}": "",
        })
        version_go = self._render("version.go.tpl", {})
        root = _go_project(tmp_path / "repo", {
            ".goreleaser.yml": goreleaser,
            "version.go": version_go,
            "main.go": _main_go(),
        })
        result = _run(root)
        assert result.status == "pass", _text(result)


# ---------------------------------------------------------------------------
# Registration: a new check is registered in five places
# ---------------------------------------------------------------------------


class TestFivePlaceRegistration:
    def test_it_is_in_the_checks_metadata_registry(self):
        import tomllib

        with open(os.path.join(REPO_ROOT, "rlsbl", "data", "checks.toml"), "rb") as f:
            checks = tomllib.load(f)["checks"]
        meta = checks["ldflags-symbol"]
        assert meta["severity"] == "error"
        assert "project" in meta["tags"]
        assert "preflight" in meta["tags"]
        # It reads nothing outside the working tree, so it must NOT join the
        # networked release family.
        assert "release" not in meta["tags"]
        assert meta["needs_network"] is False

    def test_it_is_in_the_check_to_target_matrix(self):
        assert CHECK_TARGETS["ldflags-symbol"] == frozenset({"go"})

    def test_it_has_a_row_in_the_docs_check_reference(self):
        text = open(
            os.path.join(REPO_ROOT, ".stricttools", "docs", "checks.md"), encoding="utf-8",
        ).read()
        assert "| `ldflags-symbol` |" in text

    def test_it_is_in_the_expected_checks_roster(self):
        from test_doctor_checks_migration import EXPECTED_CHECKS

        assert "ldflags-symbol" in EXPECTED_CHECKS

    def test_it_is_registered_on_the_app(self):
        assert app._check_defs["ldflags-symbol"].impl is not None
