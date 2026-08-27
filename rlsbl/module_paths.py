"""The one module-prefix containment rule.

"Is import path *X* inside module *Y*?" is asked by the Go import scanner, the
dead-module/dead-package detectors, the strictcli entry-point detector and the
``rlsbl rewrite go-module-path`` sweep.  Every one of them used to spell the
answer inline, and one of the spellings was wrong.

The rule, stated once
---------------------

A path is inside a module prefix when it IS the prefix, or when it continues
past the prefix at a **separator boundary**.  The separator is the ecosystem's
own: ``/`` for Go import paths, ``.`` for Python and JVM dotted names.

The boundary is the whole rule.  A bare ``path.startswith(prefix)`` also
matches a DIFFERENT module whose name merely begins with the same letters --
``github.com/o/foo`` matched ``github.com/o/foobar``, and
``strictcli`` matched ``strictcli-extras`` -- which is a wrong answer, not a
conservative one: it makes an unrelated third-party module look like the one
being asked about.

There is deliberately no default separator.  A caller that does not say which
ecosystem's paths it is comparing has not decided, and a wrong default silently
turns dotted names into never-matching ones (or the reverse).
"""

#: Separator between components of a Go import path.
GO_SEP = "/"

#: Separator between components of a Python / Java / Kotlin dotted name.
DOT_SEP = "."


def under_module_prefix(path: str, prefix: str, *, sep: str) -> bool:
    """True when *path* is *prefix* itself or lies beneath it.

    Args:
        path: the candidate import path (``github.com/o/foo/bar``, ``a.b.c``).
        prefix: the module path to test containment against.
        sep: the ecosystem's path separator -- :data:`GO_SEP` or
            :data:`DOT_SEP`.  Required: see the module docstring.

    An empty *prefix* matches nothing.  Containment is asked of the pair as
    given; neither side is normalized, because a module path's case and
    punctuation are significant in both ecosystems this serves.
    """
    if not prefix:
        return False
    return path == prefix or path.startswith(prefix + sep)


def go_import_under_module(import_path: str, module_path: str) -> bool:
    """True when a Go *import_path* belongs to the module at *module_path*."""
    return under_module_prefix(import_path, module_path, sep=GO_SEP)


def dotted_under_module(name: str, prefix: str) -> bool:
    """True when a dotted *name* (Python, Java, Kotlin) is inside *prefix*."""
    return under_module_prefix(name, prefix, sep=DOT_SEP)


def rewrite_module_prefix(path: str, old_prefix: str, new_prefix: str, *, sep: str) -> str:
    """Re-root *path* from *old_prefix* onto *new_prefix*.

    Returns *path* unchanged when it is not under *old_prefix*, so a caller can
    map a whole import list through this without pre-filtering.
    """
    if not under_module_prefix(path, old_prefix, sep=sep):
        return path
    return new_prefix + path[len(old_prefix):]
