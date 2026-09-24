"""Offline judgement of a Go package name: the identifier callers type, as in ``testsandbox.Run``.

``rlsbl check-name --target go`` asks this module and nothing else. A Go module
path lives under its owner, so there is no shared namespace to query; what a
candidate can collide with is the language and its standard library, both of
which are known without a network:

- **invalid** (the Go spec refuses it as a package clause): not an identifier
  (a letter or ``_`` followed by letters, digits, and ``_``), a keyword, or the
  blank identifier ``_``.
- **taken**: the name of a standard-library package, i.e. the last element of
  its import path (the element before a ``/vN`` major-version suffix). Every
  file importing both packages must alias one of them. The list is the
  committed table ``rlsbl/data/go-stdlib-packages.json``, generated from
  ``go list std`` by ``scripts/gen_go_stdlib_table.py`` -- never a run-time
  call to a local toolchain.
- **discouraged** (legal, but against convention): uppercase letters or
  underscores (Effective Go: "packages are given lower case, single-word names;
  there should be no need for underscores or mixedCaps"), or a predeclared
  identifier such as ``len`` or ``string``, which the package name shadows in
  every importing file.

Precedence is invalid, then taken, then discouraged; the reason reported is
the first problem found, and the note lists every problem.
"""

import functools
import json
import os
import re
import unicodedata

TABLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "go-stdlib-packages.json")

# https://go.dev/ref/spec#Keywords
GO_KEYWORDS = frozenset({
    "break", "case", "chan", "const", "continue", "default", "defer", "else",
    "fallthrough", "for", "func", "go", "goto", "if", "import", "interface",
    "map", "package", "range", "return", "select", "struct", "switch", "type",
    "var",
})

# https://go.dev/ref/spec#Predeclared_identifiers
GO_PREDECLARED = frozenset({
    # types
    "any", "bool", "byte", "comparable", "complex64", "complex128", "error",
    "float32", "float64", "int", "int8", "int16", "int32", "int64", "rune",
    "string", "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
    # constants and the zero value
    "true", "false", "iota", "nil",
    # functions
    "append", "cap", "clear", "close", "complex", "copy", "delete", "imag",
    "len", "make", "max", "min", "new", "panic", "print", "println", "real",
    "recover",
})

STATUS_AVAILABLE = "available"
STATUS_TAKEN = "taken"
STATUS_INVALID = "invalid"
STATUS_DISCOURAGED = "discouraged"

_MAJOR_VERSION_ELEMENT = re.compile(r"v[2-9]\d*|v1\d+")


@functools.cache
def stdlib_import_paths():
    """The committed table's import paths, in table order."""
    with open(TABLE_PATH, encoding="utf-8") as handle:
        return tuple(json.load(handle)["packages"])


def package_name_of(import_path):
    """The package name an import path conventionally declares.

    The last element, except that a major-version suffix (``math/rand/v2``)
    names the element before it.
    """
    parts = import_path.split("/")
    if len(parts) > 1 and _MAJOR_VERSION_ELEMENT.fullmatch(parts[-1]):
        return parts[-2]
    return parts[-1]


def stdlib_packages_named(name):
    """Every standard-library import path whose package name is ``name``, sorted."""
    return sorted(p for p in stdlib_import_paths() if package_name_of(p) == name)


def _is_letter(ch):
    return ch == "_" or unicodedata.category(ch).startswith("L")


def _is_digit(ch):
    return unicodedata.category(ch) == "Nd"


def is_go_identifier(name):
    """True when ``name`` is an identifier per https://go.dev/ref/spec#Identifiers."""
    if not name or not _is_letter(name[0]):
        return False
    return all(_is_letter(ch) or _is_digit(ch) for ch in name[1:])


def _invalid(name, reason, why):
    return {
        "status": STATUS_INVALID,
        "reason": reason,
        "conflicts": [],
        "note": (
            f"{why}, so it cannot be a Go package name. A module or directory "
            f"named '{name}' needs a package clause that differs from that path "
            "element, and importers see that other name; rlsbl does not choose it."
        ),
    }


def check_go_package_name(name):
    """Judge ``name`` as a Go package name, offline.

    Returns ``{"status", "reason", "conflicts", "note"}``: ``status`` is one of
    ``available``, ``taken``, ``invalid`` or ``discouraged``; ``reason`` is a
    stable token (``None`` when available); ``conflicts`` lists the colliding
    standard-library import paths (``taken`` only); ``note`` is a sentence, or
    ``None`` when available.
    """
    if not is_go_identifier(name):
        bad = sorted({ch for ch in name if not (_is_letter(ch) or _is_digit(ch))})
        if not name:
            why = "The empty string is not a Go identifier"
        elif bad:
            listed = ", ".join(f"'{ch}'" for ch in bad)
            why = f"'{name}' is not a Go identifier ({listed} not allowed)"
        else:
            why = f"'{name}' is not a Go identifier (it must start with a letter or '_')"
        return _invalid(name, "not-identifier", why)
    if name in GO_KEYWORDS:
        return _invalid(name, "keyword", f"'{name}' is a Go keyword")
    if name == "_":
        return _invalid(name, "blank", "'_' is the blank identifier")

    conflicts = stdlib_packages_named(name)
    if conflicts:
        listed = ", ".join(f"'{p}'" for p in conflicts)
        return {
            "status": STATUS_TAKEN,
            "reason": "stdlib",
            "conflicts": conflicts,
            "note": (
                f"conflicts with Go standard library package {listed}: every "
                "file importing both must rename one with an import alias"
            ),
        }

    problems = []
    if any(ch.isupper() for ch in name):
        problems.append(("uppercase", "it contains uppercase letters (Go package names are lowercase)"))
    if "_" in name:
        problems.append(("underscore", "it contains an underscore (Effective Go: no underscores or mixedCaps)"))
    if name in GO_PREDECLARED:
        problems.append((
            "predeclared",
            f"it is the predeclared identifier '{name}', which the package name shadows in every importing file",
        ))
    if problems:
        return {
            "status": STATUS_DISCOURAGED,
            "reason": problems[0][0],
            "conflicts": [],
            "note": "legal but discouraged: " + "; ".join(text for _, text in problems),
        }

    return {"status": STATUS_AVAILABLE, "reason": None, "conflicts": [], "note": None}
