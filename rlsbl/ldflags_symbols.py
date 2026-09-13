"""Does every ``-X importpath.Symbol=value`` linker flag name a symbol that exists?

``go build -ldflags "-X importpath.Symbol=value"`` overwrites a package-level
string variable at link time. When the named symbol does not exist, **the link
succeeds silently and the flag does nothing** -- no error, no warning, no
diagnostic anywhere in the toolchain. A build configuration that says ``-X
main.Version=...`` while the Go source declares ``var version`` therefore ships
binaries that report their fallback version forever, and the only way to notice
is to run the released binary and read its output.

That is a coupling between two files nothing else compares: the build
configuration's chosen symbol name and the Go source's declaration. rlsbl
already polices two couplings of the same kind -- ``go-module-identity`` (a
``go.mod`` module path against the repository's origin identity) and
``dep-locks`` (a lockfile against the manifest beside it) -- and this is the
third.

What is verified
----------------

Every ``-X`` occurrence in the project's tracked build configuration
(``.goreleaser.yml``/``.yaml``, Makefiles, shell scripts, CI workflow YAML) is
resolved to a directory and the symbol is looked up in the Go source there:

* **Error** -- the symbol is not declared, or is declared as something the
  linker cannot set (a const, a function, a non-string var, a var initialized
  to a non-constant expression). The linker can only set a package-level
  ``var`` of type string that is uninitialized or initialized to a constant
  string.
* **Warn** -- the symbol exists and can be set, but nothing in the module reads
  it. That produces the identical user-visible bug by a different route: the
  value arrives and the version surface prints a hardcoded literal anyway.
  Warn rather than error because the fix can require adding a version surface,
  which is a larger change than a rename.

Symbol-agnostic on purpose
--------------------------

The RELATIONSHIP is checked, never a naming convention. ``main.version`` with
``var version`` and ``main.Version`` with ``var Version`` are both correct;
enforcing either spelling would force churn on working projects and would still
miss mismatches in the other direction.

What it refuses to guess
------------------------

An occurrence whose target cannot be resolved is reported as unverified (a
note) rather than guessed at: a build-time template in the import path
(``-X {{ .Env.MODULE }}/cmd/x.Version=...``), a package outside this module,
and a bare ``main`` in a build file that names no package and whose module has
several main packages. Guessing there would mint errors against code that is
correct.
"""

import os
import re
from dataclasses import dataclass, field

from .go_identity import read_module_line

#: Directories never scanned for build configuration: scaffold base copies
#: (which are not live configuration) and goreleaser's own output.
_EXCLUDED_PREFIXES = (".rlsbl/bases/", "dist/")

#: goreleaser's document, in both spellings and with or without the dot.
_GORELEASER_NAMES = frozenset({
    ".goreleaser.yml", ".goreleaser.yaml", "goreleaser.yml", "goreleaser.yaml",
})

#: A ``-X`` target: an import path plus ``.Symbol``, possibly carrying a
#: build-time template element (``{{ .Env.MODULE }}``) that names no directory.
_TARGET = r"(?:\{\{[^{}]*\}\}|[^\s\"'=])+"

#: ``-X <target>=``, with the separator the linker requires after ``-X`` and a
#: left boundary so a longer flag ending in ``-X`` never matches.
_X_FLAG = re.compile(r"(?<![\w-])-X[=\s]\s*[\"']?(" + _TARGET + r")=")

#: ``go build``/``go install`` target arguments: ``.``, ``./cmd/tool``.
_GO_BUILD = re.compile(r"\bgo\s+(?:build|install)\b([^\n;&|]*)")
_PACKAGE_ARG = re.compile(r"(?<![\w/.])(\.(?:/[\w.@-]+)*)(?![\w/.])")

_GO_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$")

#: Both Go string literal node types -- ``var V = `dev` `` is as constant as
#: ``var V = "dev"``.
_STRING_LITERALS = ("interpreted_string_literal", "raw_string_literal")


@dataclass(frozen=True)
class Occurrence:
    """One ``-X`` flag found in a build file."""

    file: str          # relative to the module directory
    line: int          # 1-based
    target: str        # the raw "importpath.Symbol" text
    import_path: str | None   # None when the target carries a template
    symbol: str | None


@dataclass(frozen=True)
class Declaration:
    """What a Go package declares under one name."""

    file: str          # relative to the module directory
    line: int          # 1-based
    kind: str          # "var", "const", "func", "type"
    description: str   # human-readable, for the message
    injectable: bool   # can the linker set it: a package-level string var
    name_offsets: tuple[int, ...] = ()   # byte offsets of the name node(s)


@dataclass
class LdflagsVerdict:
    """Result of comparing every ``-X`` target against the Go source."""

    #: Targets that name nothing the linker can set -- each one fails the check.
    problems: list = field(default_factory=list)
    #: Targets the linker can set but nothing in the module reads.
    warnings: list = field(default_factory=list)
    #: Targets left unverified, each carrying why it could not be resolved.
    notes: list = field(default_factory=list)
    #: Why there was nothing to compare, when the check did not run at all.
    skip_reason: str | None = None
    #: How many targets were resolved to a symbol the linker can set.
    verified: int = 0

    @property
    def ok(self):
        return not self.problems


# ---------------------------------------------------------------------------
# Tracked build files
# ---------------------------------------------------------------------------


def git_tracked_files(directory):
    """Every path git tracks under *directory*, relative to it.

    Only tracked files are this project's build configuration: an untracked
    script is one developer's scratch copy, and a gitignored one is not shipped
    at all.
    """
    from .utils import run

    out = run("git", ["ls-files", "-z"], cwd=directory)
    return [p for p in out.split("\0") if p]


def is_build_file(rel_path):
    """Is *rel_path* a file that can carry linker flags?"""
    rel = rel_path.replace(os.sep, "/")
    if rel.startswith(_EXCLUDED_PREFIXES) or any(
        f"/{prefix}" in f"/{rel}" for prefix in _EXCLUDED_PREFIXES
    ):
        return False
    base = os.path.basename(rel)
    lowered = base.lower()
    if lowered in _GORELEASER_NAMES:
        return True
    if lowered in ("makefile", "gnumakefile") or lowered.endswith(".mk"):
        return True
    if lowered.endswith((".sh", ".bash")):
        return True
    parts = rel.split("/")
    if (
        ".github" in parts
        and "workflows" in parts
        and lowered.endswith((".yml", ".yaml"))
    ):
        return True
    return False


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def split_target(target):
    """Split ``importpath.Symbol`` into its two halves, or ``(None, None)``.

    The linker splits at the LAST dot, so an import path carrying dots
    (``github.com/owner/repo/internal/version``) resolves the same way the
    toolchain resolves it. A target carrying a build-time template names no
    directory and is refused here rather than guessed at.
    """
    if "{{" in target:
        return None, None
    path, sep, symbol = target.rpartition(".")
    if not sep or not path or not _GO_IDENTIFIER.match(symbol):
        return None, None
    return path, symbol


def find_occurrences(text, rel_path):
    """Every ``-X`` occurrence in *text*, with 1-based line numbers."""
    found = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _X_FLAG.finditer(line):
            target = match.group(1)
            path, symbol = split_target(target)
            found.append(Occurrence(
                file=rel_path, line=lineno, target=target,
                import_path=path, symbol=symbol,
            ))
    return found


# ---------------------------------------------------------------------------
# Where a `-X` target points
# ---------------------------------------------------------------------------


def _normalize_main(value):
    """A goreleaser ``main:`` value as a directory relative to the module root.

    goreleaser accepts a directory (``./cmd/tool``) or a file
    (``./cmd/tool/main.go``); both name the same package.
    """
    text = (value or ".").strip()
    if text.endswith(".go"):
        text = os.path.dirname(text) or "."
    rel = os.path.normpath(text).replace(os.sep, "/")
    return "." if rel in ("", ".") else rel.lstrip("./") or "."


def goreleaser_builds(text):
    """``[(ldflags_text, main_dir)]`` for each declared build, or None.

    None means the document could not be read as goreleaser's -- the caller
    then resolves a bare ``main`` the way it resolves one in a shell script.
    """
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.error import YAMLError
    except ImportError:  # pragma: no cover - ruamel is a hard dependency
        return None
    try:
        doc = YAML(typ="safe").load(text)
    except YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    builds = doc.get("builds")
    if not isinstance(builds, list):
        return None
    entries = []
    for build in builds:
        if not isinstance(build, dict):
            continue
        ldflags = build.get("ldflags")
        if isinstance(ldflags, str):
            rendered = ldflags
        elif isinstance(ldflags, list):
            rendered = " ".join(str(item) for item in ldflags)
        else:
            rendered = ""
        entries.append((rendered, _normalize_main(build.get("main"))))
    return entries or None


def _explicit_build_target(text, line_index):
    """The package a ``go build``/``go install`` names, nearest first.

    The occurrence's own line is consulted first (the common
    ``go build -ldflags "-X main.V=$(V)" ./cmd/tool``), then the rest of the
    file, which covers a Makefile holding its flags in a variable.
    """
    lines = text.splitlines()
    order = [line_index] + [i for i in range(len(lines)) if i != line_index]
    for index in order:
        if index >= len(lines):
            continue
        for match in _GO_BUILD.finditer(lines[index]):
            args = match.group(1)
            packages = [
                p for p in _PACKAGE_ARG.findall(args) if not p.endswith("...")
            ]
            if packages:
                return _normalize_main(packages[-1])
    return None


# ---------------------------------------------------------------------------
# The Go source
# ---------------------------------------------------------------------------


class _GoSource:
    """Parsed Go source for one module, with per-directory symbol tables."""

    def __init__(self, module_dir):
        self.module_dir = module_dir
        self._parser = None
        self._trees = {}
        self._symbols = {}
        self._go_files = None

    # -- parsing -----------------------------------------------------------

    def _parse(self, rel_path):
        if rel_path in self._trees:
            return self._trees[rel_path]
        if self._parser is None:
            import tree_sitter_go
            from tree_sitter import Language, Parser

            self._parser = Parser(Language(tree_sitter_go.language()))
        try:
            with open(os.path.join(self.module_dir, rel_path), "rb") as f:
                source = f.read()
        except OSError:
            self._trees[rel_path] = None
            return None
        tree = self._parser.parse(source)
        self._trees[rel_path] = tree
        return tree

    def go_files(self):
        """Every non-test ``.go`` file in the module, module-relative.

        ``vendor/`` and ``testdata/`` are other people's code and fixtures; a
        symbol declared there is not this module's.
        """
        if self._go_files is not None:
            return self._go_files
        collected = []
        for dirpath, dirnames, filenames in os.walk(self.module_dir):
            dirnames[:] = [
                d for d in dirnames
                if d not in (".git", "vendor", "testdata", "node_modules")
            ]
            for name in filenames:
                if not name.endswith(".go") or name.endswith("_test.go"):
                    continue
                rel = os.path.relpath(
                    os.path.join(dirpath, name), self.module_dir,
                )
                collected.append(rel.replace(os.sep, "/"))
        self._go_files = sorted(collected)
        return self._go_files

    def files_in(self, rel_dir):
        """The module's non-test Go files directly in *rel_dir*."""
        target = "." if rel_dir in ("", ".") else rel_dir.rstrip("/")
        return [
            path for path in self.go_files()
            if (os.path.dirname(path) or ".") == target
        ]

    # -- declarations ------------------------------------------------------

    def symbols(self, rel_dir):
        """``{name: Declaration}`` for the package-level declarations there."""
        key = "." if rel_dir in ("", ".") else rel_dir.rstrip("/")
        if key in self._symbols:
            return self._symbols[key]
        table = {}
        for path in self.files_in(key):
            tree = self._parse(path)
            if tree is None:
                continue
            for node in tree.root_node.children:
                for name, decl in _declarations(node, path):
                    table.setdefault(name, decl)
        self._symbols[key] = table
        return table

    def string_var_names(self, rel_dir):
        return sorted(
            name for name, decl in self.symbols(rel_dir).items()
            if decl.injectable
        )

    # -- references --------------------------------------------------------

    def is_read(self, symbol, decl):
        """Does anything in the module reference *symbol* beyond its declaration?

        Module-wide rather than package-local, because a symbol injected into a
        library package (``-X .../internal/version.Value``) is read from the
        binary that imports it. A cross-package read is a ``field_identifier``
        (``version.Value``), so both identifier kinds count.
        """
        wanted = symbol.encode("utf-8")
        for path in self.go_files():
            tree = self._parse(path)
            if tree is None:
                continue
            excluded = decl.name_offsets if path == decl.file else ()
            if _references(tree.root_node, wanted, excluded):
                return True
        return False


def _declarations(node, rel_path):
    """``(name, Declaration)`` pairs for one top-level node."""
    line = node.start_point[0] + 1
    if node.type == "var_declaration":
        for spec in _var_specs(node):
            yield from _var_spec_declarations(spec, rel_path)
    elif node.type == "const_declaration":
        for spec in node.children:
            for inner in _const_specs(spec):
                for name_node in inner.children_by_field_name("name"):
                    yield name_node.text.decode("utf-8"), Declaration(
                        file=rel_path, line=inner.start_point[0] + 1,
                        kind="const", description="a const",
                        injectable=False,
                    )
    elif node.type == "function_declaration":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            yield name_node.text.decode("utf-8"), Declaration(
                file=rel_path, line=line, kind="func",
                description="a function", injectable=False,
            )
    elif node.type == "type_declaration":
        for spec in node.children:
            name_node = (
                spec.child_by_field_name("name")
                if spec.type in ("type_spec", "type_alias") else None
            )
            if name_node is not None:
                yield name_node.text.decode("utf-8"), Declaration(
                    file=rel_path, line=spec.start_point[0] + 1, kind="type",
                    description="a type", injectable=False,
                )


def _var_specs(node):
    for child in node.children:
        if child.type == "var_spec":
            yield child
        elif child.type == "var_spec_list":
            for inner in child.children:
                if inner.type == "var_spec":
                    yield inner


def _const_specs(node):
    if node.type == "const_spec":
        yield node
    elif node.type == "const_spec_list":
        for inner in node.children:
            if inner.type == "const_spec":
                yield inner


def _var_spec_declarations(spec, rel_path):
    """What one ``var`` spec declares, and whether the linker can set it."""
    line = spec.start_point[0] + 1
    type_node = spec.child_by_field_name("type")
    value_node = spec.child_by_field_name("value")
    names = spec.children_by_field_name("name")

    type_text = type_node.text.decode("utf-8") if type_node is not None else None
    values = list(value_node.children) if value_node is not None else []
    values = [v for v in values if v.type not in (",",)]

    for index, name_node in enumerate(names):
        value = values[index] if index < len(values) else None
        literal = value is not None and value.type in _STRING_LITERALS
        if type_text is not None and type_text != "string":
            injectable, description = False, f"a var of type `{type_text}`"
        elif value is not None and not literal:
            injectable, description = False, (
                "a var initialized to a non-constant expression"
            )
        elif type_text is None and value is None:
            # `var a, b = f()` shapes with fewer values than names.
            injectable, description = False, (
                "a var whose type cannot be read as string"
            )
        else:
            injectable, description = True, "a package-level string var"
        yield name_node.text.decode("utf-8"), Declaration(
            file=rel_path, line=line, kind="var", description=description,
            injectable=injectable, name_offsets=(name_node.start_byte,),
        )


def _references(node, wanted, excluded_offsets):
    """Is *wanted* used as an identifier anywhere under *node*?"""
    stack = [node]
    while stack:
        current = stack.pop()
        if (
            current.type in ("identifier", "field_identifier")
            and current.text == wanted
            and current.start_byte not in excluded_offsets
        ):
            return True
        stack.extend(current.children)
    return False


# ---------------------------------------------------------------------------
# The evaluation
# ---------------------------------------------------------------------------


def _dir_label(rel_dir):
    return "the module root" if rel_dir in ("", ".") else f"`{rel_dir}`"


def _missing_symbol_problem(occ, rel_dir, source):
    declared = source.string_var_names(rel_dir)
    if declared:
        inventory = (
            "Package-level string vars declared there: "
            + ", ".join(f"`{name}`" for name in declared) + "."
        )
    else:
        inventory = "That directory declares no package-level string var at all."
    return (
        f"{occ.file}:{occ.line}: the linker is told to set `{occ.target}` "
        f"(-X), but {_dir_label(rel_dir)} declares no package-level "
        f"`{occ.symbol}`. A -X flag naming a symbol that does not exist links "
        f"SILENTLY -- nothing is set, and every built binary keeps the "
        f"fallback value in the code. {inventory} Rename the Go variable to "
        f"`{occ.symbol}`, or change the -X target to a name the package "
        f"declares."
    )


def _uninjectable_problem(occ, rel_dir, decl):
    return (
        f"{occ.file}:{occ.line}: the linker is told to set `{occ.target}` "
        f"(-X), but {decl.file}:{decl.line} declares `{occ.symbol}` as "
        f"{decl.description} -- the linker can only set a package-level `var` "
        f"of type string that is uninitialized or initialized to a constant "
        f"string, so this flag links SILENTLY and sets nothing. Declare it as "
        f"`var {occ.symbol} string` in {_dir_label(rel_dir)}, or change the -X "
        f"target to a symbol that is one."
    )


def _unread_warning(occ, decl):
    return (
        f"{occ.file}:{occ.line}: `{occ.target}` is declared at {decl.file}:"
        f"{decl.line} and the linker can set it, but nothing in this module "
        f"reads it -- the injected value goes nowhere and the binary reports "
        f"whatever the code uses instead. Read `{occ.symbol}` where the "
        f"version is reported, or drop the -X flag."
    )


def _resolve_directory(occ, text, builds, module_path, source):
    """``(rel_dir, reason)``: where the target points, or why it is unverified."""
    if occ.import_path is None:
        return None, (
            "the import path carries a build-time template, so it names no "
            "directory"
        )

    if occ.import_path != "main":
        if not module_path:
            return None, (
                "this module's go.mod declares no module path, so a qualified "
                "import path cannot be resolved to a directory"
            )
        if occ.import_path == module_path:
            return ".", None
        if occ.import_path.startswith(module_path + "/"):
            return occ.import_path[len(module_path) + 1:], None
        return None, (
            f"`{occ.import_path}` is outside this module ({module_path}), so "
            f"its source is not here to compare against"
        )

    # A bare `main` names the main package this build command builds.
    if builds is not None:
        matching = {main for flags, main in builds if occ.target in flags}
        if not matching:
            matching = {main for _, main in builds}
        if len(matching) == 1:
            return matching.pop(), None
        return None, (
            "several goreleaser builds declare different main packages and "
            "none of them owns this flag, so `main` names no one directory"
        )

    explicit = _explicit_build_target(text, occ.line - 1)
    if explicit is not None:
        return explicit, None

    mains = _main_package_dirs(source)
    if "." in mains:
        return ".", None
    if len(mains) == 1:
        return mains[0], None
    if not mains:
        return None, (
            "this module declares no `package main`, so a bare `main` import "
            "path names no directory"
        )
    return None, (
        "this file names no package to build and the module has several main "
        "packages ("
        + ", ".join(mains)
        + "), so `main` names no one directory"
    )


def _main_package_dirs(source):
    """Module-relative directories declaring ``package main``."""
    dirs = []
    for path in source.go_files():
        tree = source._parse(path)
        if tree is None:
            continue
        for node in tree.root_node.children:
            if node.type != "package_clause":
                continue
            name = node.children[-1].text.decode("utf-8")
            if name == "main":
                rel = os.path.dirname(path) or "."
                if rel not in dirs:
                    dirs.append(rel)
    return dirs


def evaluate_ldflags_symbols(module_dirs, *, list_tracked=None):
    """Compare every ``-X`` target in the build configuration against the source.

    *module_dirs* are absolute directories expected to contain a ``go.mod``.
    *list_tracked* enumerates the tracked paths under one of them, relative to
    it; it defaults to :func:`git_tracked_files`.
    """
    enumerate_tracked = list_tracked or git_tracked_files

    if not module_dirs:
        return LdflagsVerdict(skip_reason="no Go module in this project")

    verdict = LdflagsVerdict()
    warned = set()
    modules = 0
    for directory in module_dirs:
        go_mod = os.path.join(directory, "go.mod")
        if not os.path.isfile(go_mod):
            continue
        modules += 1
        module_path = read_module_line(go_mod)
        source = _GoSource(directory)
        _evaluate_module(
            directory, module_path, source, enumerate_tracked, verdict, warned,
        )

    if not modules:
        return LdflagsVerdict(skip_reason="no go.mod found in any Go target")
    return verdict


def _evaluate_module(directory, module_path, source, enumerate_tracked,
                     verdict, warned):
    for rel_path in sorted(enumerate_tracked(directory)):
        if not is_build_file(rel_path):
            continue
        text = _read_text(os.path.join(directory, rel_path))
        if text is None or "-X" not in text:
            continue
        occurrences = find_occurrences(text, rel_path)
        if not occurrences:
            continue
        builds = (
            goreleaser_builds(text)
            if os.path.basename(rel_path).lower() in _GORELEASER_NAMES
            else None
        )
        for occ in occurrences:
            rel_dir, reason = _resolve_directory(
                occ, text, builds, module_path, source,
            )
            if rel_dir is None:
                verdict.notes.append(
                    f"{occ.file}:{occ.line}: `{occ.target}` was not verified: "
                    f"{reason}."
                )
                continue
            decl = source.symbols(rel_dir).get(occ.symbol)
            if decl is None:
                verdict.problems.append(
                    _missing_symbol_problem(occ, rel_dir, source)
                )
                continue
            if not decl.injectable:
                verdict.problems.append(
                    _uninjectable_problem(occ, rel_dir, decl)
                )
                continue
            verdict.verified += 1
            key = (directory, rel_dir, occ.symbol)
            if key in warned or source.is_read(occ.symbol, decl):
                continue
            warned.add(key)
            verdict.warnings.append(_unread_warning(occ, decl))
