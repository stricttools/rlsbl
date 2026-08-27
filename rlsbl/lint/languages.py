"""The single table of languages the library linter knows about.

This taxonomy is deliberately NOT the release-target taxonomy. ``python`` is
not ``pypi``: a language is a way of writing source files, a target is a way of
publishing them, and one language can back several targets (Kotlin and Java
both arrive here as ``maven``). Keeping the two apart is the point -- the
mapping between them is a single property on the target
(``ReleaseTarget.lint_language``), not a name comparison repeated per call
site.

Before this table, the same four language names were spelled out in four
separate places: ``_detect_languages`` (manifest -> language),
``_create_linter`` (language -> linter class), ``_create_import_scanner``
(language -> scanner class), and ``_DEFAULT_EXCLUDE_PATTERNS`` (language ->
excludes). Each one could drift from the others, and two of them answered an
unknown language with a bare ``return None`` that the caller silently ignored.
Everything now derives from ``LANGUAGES``, and an unknown language is a hard
error rather than a quiet no-op.
"""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class LintLanguage:
    """Everything the linter knows about one language.

    Attributes:
        name: the language identifier used in config filenames
            (``.rlsbl/lint/<name>.toml``) and in ``LanguageLinter.language``.
        manifests: filenames whose presence in a project root means this
            language is present. Any one of them is enough.
        default_excludes: glob patterns excluded from lint by default, so
            boundary checks look at production code rather than tests and
            examples.
        default_forbidden_imports: the imports a library of this language must
            not reach for, applied when the project ships no
            ``.rlsbl/lint/<name>.toml``. Empty for languages whose linter
            shells out to the project's own tool.
        linters: maps a parser type to a zero-argument factory. The
            ``"default"`` key is used when the project declares no parser
            preference; ``"regex"`` is the opt-out for environments without
            a tree-sitter grammar. A language offering only one
            implementation lists it under both keys.
        import_scanner: factory for the AST-based import scanner, or None.
        scanner_absent_reason: why ``import_scanner`` is None. Required
            whenever it is -- an absent scanner must state its absence, not
            be an unexplained blank.
    """

    name: str
    manifests: tuple[str, ...]
    default_excludes: tuple[str, ...]
    linters: dict[str, Callable[[], object]]
    default_forbidden_imports: tuple[str, ...] = ()
    import_scanner: Callable[[], object] | None = None
    scanner_absent_reason: str | None = None

    def __post_init__(self):
        if self.import_scanner is None and not self.scanner_absent_reason:
            raise ValueError(
                f"lint language '{self.name}' has no import scanner and no "
                f"reason for its absence; state the reason"
            )
        for key in ("default", "regex"):
            if key not in self.linters:
                raise ValueError(
                    f"lint language '{self.name}' declares no '{key}' linter"
                )

    def detect(self, project_path: str) -> bool:
        """Return True when any declared manifest exists in *project_path*."""
        import os

        return any(
            os.path.isfile(os.path.join(project_path, m)) for m in self.manifests
        )

    def linter(self, parser_type: str):
        """Instantiate the linter for *parser_type*, defaulting to AST."""
        factory = self.linters.get(parser_type) or self.linters["default"]
        return factory()


def _python_ast():
    from .python_ast import PythonAstLinter

    return PythonAstLinter()


def _python_regex():
    from .python_regex import PythonRegexLinter

    return PythonRegexLinter()


def _go_ast():
    from .go_ast import GoAstLinter

    return GoAstLinter()


def _go_regex():
    from .go_regex import GoRegexLinter

    return GoRegexLinter()


def _npm_ast():
    from .npm_ast import NpmAstLinter

    return NpmAstLinter()


def _npm_regex():
    from .npm_regex import NpmRegexLinter

    return NpmRegexLinter()


def _maven():
    from .maven import MavenLinter

    return MavenLinter()


LANGUAGES: tuple[LintLanguage, ...] = (
    LintLanguage(
        name="python",
        manifests=("pyproject.toml",),
        default_excludes=("tests/", "test_*.py", "conftest.py", "examples/"),
        linters={"default": _python_ast, "ast": _python_ast, "regex": _python_regex},
        default_forbidden_imports=(
            "argparse", "click", "typer",
            "flask", "fastapi", "django",
            "uvicorn", "granian", "starlette",
            "tornado", "bottle",
        ),
        import_scanner=_python_ast,
    ),
    LintLanguage(
        name="go",
        manifests=("go.mod",),
        default_excludes=("*_test.go", "examples/"),
        linters={"default": _go_ast, "ast": _go_ast, "regex": _go_regex},
        default_forbidden_imports=(
            "net/http",
            "github.com/spf13/cobra",
            "github.com/urfave/cli",
        ),
        import_scanner=None,
        scanner_absent_reason=(
            "Go imports are scanned per file by rlsbl.lint.go_ast.scan_imports, "
            "which dep_validation drives directly; there is no project-wide "
            "scanner object for this language"
        ),
    ),
    LintLanguage(
        name="npm",
        manifests=("package.json",),
        default_excludes=(
            "__tests__/", "*.test.js", "*.test.ts",
            "*.spec.js", "*.spec.ts", "examples/",
        ),
        linters={"default": _npm_ast, "ast": _npm_ast, "regex": _npm_regex},
        default_forbidden_imports=(
            "express", "koa", "hono",
            "commander", "yargs",
        ),
        import_scanner=_npm_ast,
    ),
    LintLanguage(
        name="maven",
        manifests=("build.gradle.kts", "build.gradle", "pom.xml"),
        default_excludes=(),
        linters={"default": _maven, "ast": _maven, "regex": _maven},
        import_scanner=None,
        scanner_absent_reason=(
            "the Maven linter shells out to the project's own tool (detekt, "
            "checkstyle, gradlew check); rlsbl never parses JVM sources itself"
        ),
    ),
)

LANGUAGES_BY_NAME: dict[str, LintLanguage] = {lang.name: lang for lang in LANGUAGES}


def get_language(name: str) -> LintLanguage:
    """Return the declared language, or raise naming the unknown one.

    Deliberately not ``.get(name, None)``: a caller that reached here with a
    language the table does not declare has a bug, and returning None would
    turn that bug into a silently skipped lint.
    """
    try:
        return LANGUAGES_BY_NAME[name]
    except KeyError:
        known = ", ".join(sorted(LANGUAGES_BY_NAME))
        raise ValueError(
            f"unknown lint language '{name}'; declared languages are {known}"
        ) from None
