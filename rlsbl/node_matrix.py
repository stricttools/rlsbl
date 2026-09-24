"""The Node versions an npm project's CI tests on, derived from ``engines.node``.

The CI matrix is every supported Node line whose newest release satisfies the
package's own ``engines.node`` range -- the version ``actions/setup-node``
installs for ``node-version: <line>`` is that line's newest release, so a line
is tested exactly when the package claims to run on what CI would install.
A package that declares no ``engines.node`` states no Node support at all, and
scaffolding its CI is refused rather than guessing a matrix.
"""

import json
import os
import re

#: The Node release lines rlsbl scaffolds CI for.
SUPPORTED_NODE_LINES = (20, 22, 24)

#: Stands in for "the newest minor/patch of a line": any real release is lower.
_NEWEST = 10**9


class NodeMatrixError(Exception):
    """The Node CI matrix cannot be derived from the package's declaration."""


_COMPARATOR = re.compile(
    r"(<=|>=|<|>|=|\^|~>|~)?\s*v?"
    r"([0-9]+|[xX*])(?:\.([0-9]+|[xX*]))?(?:\.([0-9]+|[xX*]))?"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)
_HYPHEN = re.compile(r"^(\S+)\s+-\s+(\S+)$")


def _part(text):
    return None if text is None or text in ("x", "X", "*") else int(text)


def _lower(parts):
    return tuple(p or 0 for p in parts)


def _upper_exclusive(major, minor, patch):
    """The exclusive upper bound a partial version implies (``<=1.2`` ->
    ``<1.3.0``), or the full version itself for an inclusive full bound."""
    if minor is None:
        return (major + 1, 0, 0), False
    if patch is None:
        return (major, minor + 1, 0), False
    return (major, minor, patch), True


def _comparator_bounds(op, major, minor, patch):
    """``[(kind, version)]`` bounds, kind one of ``>=``, ``>``, ``<``, ``<=``."""
    if major is None:
        return []  # "*", "x": anything
    op = op or "="
    if op == "=":
        if minor is None:
            return [(">=", (major, 0, 0)), ("<", (major + 1, 0, 0))]
        if patch is None:
            return [(">=", (major, minor, 0)), ("<", (major, minor + 1, 0))]
        return [(">=", (major, minor, patch)), ("<=", (major, minor, patch))]
    if op == ">=":
        return [(">=", _lower((major, minor, patch)))]
    if op == ">":
        if minor is None:
            return [(">=", (major + 1, 0, 0))]
        if patch is None:
            return [(">=", (major, minor + 1, 0))]
        return [(">", (major, minor, patch))]
    if op == "<":
        return [("<", _lower((major, minor, patch)))]
    if op == "<=":
        bound, inclusive = _upper_exclusive(major, minor, patch)
        return [("<=" if inclusive else "<", bound)]
    if op in ("~", "~>"):
        low = _lower((major, minor, patch))
        if minor is None:
            return [(">=", low), ("<", (major + 1, 0, 0))]
        return [(">=", low), ("<", (major, minor + 1, 0))]
    if op == "^":
        low = _lower((major, minor, patch))
        if major > 0 or minor is None:
            return [(">=", low), ("<", (major + 1, 0, 0))]
        if minor > 0 or patch is None:
            return [(">=", low), ("<", (0, minor + 1, 0))]
        return [(">=", low), ("<", (0, 0, patch + 1))]
    raise AssertionError(op)


def _parse_set(text, whole):
    """The bounds of one ``||``-separated comparator set."""
    text = text.strip()
    if text in ("", "*", "x", "X"):
        return []
    hyphen = _HYPHEN.match(text)
    if hyphen:
        low = _parse_set(hyphen.group(1), whole)
        high = _parse_set("<=" + hyphen.group(2), whole)
        return [b for b in low if b[0] in (">=", ">")] + high
    bounds = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        match = _COMPARATOR.match(text, pos)
        if match is None or match.end() == pos:
            raise NodeMatrixError(
                f"engines.node {whole!r} is not a range rlsbl can read (at "
                f"{text[pos:]!r}); write it as an npm semver range such as "
                f'">=22" or "^22 || ^24"'
            )
        op, major, minor, patch = match.groups()
        bounds.extend(_comparator_bounds(op, _part(major), _part(minor), _part(patch)))
        pos = match.end()
    return bounds


def _satisfies(version, bounds):
    for kind, bound in bounds:
        if kind == ">=" and not version >= bound:
            return False
        if kind == ">" and not version > bound:
            return False
        if kind == "<" and not version < bound:
            return False
        if kind == "<=" and not version <= bound:
            return False
    return True


def node_lines_satisfying(node_range):
    """The supported Node lines whose newest release *node_range* admits.

    Raises:
        NodeMatrixError: the range cannot be read, or admits no supported line.
    """
    sets = [_parse_set(part, node_range) for part in node_range.split("||")]
    lines = [
        line for line in SUPPORTED_NODE_LINES
        if any(_satisfies((line, _NEWEST, _NEWEST), bounds) for bounds in sets)
    ]
    if not lines:
        raise NodeMatrixError(
            f"engines.node {node_range!r} admits none of the Node lines rlsbl "
            f"tests on ({', '.join(str(n) for n in SUPPORTED_NODE_LINES)}), so "
            f"CI would test on nothing; widen the range in package.json"
        )
    return lines


def missing_engines_message(package_json_path):
    """The refusal for a package that declares no ``engines.node``."""
    return (
        f"{package_json_path} declares no engines.node, so rlsbl cannot tell "
        f"which Node versions CI must test on (it tests every supported line "
        f"-- {', '.join(str(n) for n in SUPPORTED_NODE_LINES)} -- that the "
        f"range admits). Declare the Node versions the package supports, e.g. "
        f'"engines": {{"node": ">=22"}} in package.json, and re-run rlsbl '
        f"scaffold."
    )


def project_node_matrix(project_dir):
    """The Node CI matrix of the npm package in *project_dir*.

    Raises:
        NodeMatrixError: no ``engines.node``, or one that cannot be used.
    """
    path = os.path.join(project_dir, "package.json")
    with open(path, "r", encoding="utf-8") as f:
        pkg = json.load(f)
    engines = pkg.get("engines")
    node_range = engines.get("node") if isinstance(engines, dict) else None
    if not isinstance(node_range, str) or not node_range.strip():
        raise NodeMatrixError(missing_engines_message(path))
    return node_lines_satisfying(node_range)


def render_matrix(lines):
    """The YAML flow sequence a workflow's ``node-version`` takes."""
    return "[" + ", ".join(str(line) for line in lines) + "]"
