"""Does each lockfile still resolve the manifest beside it?

The sibling failure class to :mod:`rlsbl.dep_floors`. That module asks whether
a DECLARED floor is behind what the lock resolved; this one asks the question
underneath it: does the lock resolve THIS manifest at all, or was a dependency
added, removed or re-constrained after the lock was written?

A stale lock is not a cosmetic problem. ``uv sync``/``npm ci`` install what the
lock says, so CI and every contributor keep resolving the old dependency set
while the manifest advertises a new one; ``dep-floors`` compares against a lock
that no longer describes the manifest; and the release refreshes the lock at
bump time, so the drift surfaces as an unrelated diff in the release commit.

Offline and structural, on purpose
----------------------------------

Nothing here runs a package manager and nothing touches the network. The
obvious alternative -- shelling out to ``uv lock --check`` -- was rejected:

* it is a resolver invocation, so it can reach the index (and its exit code
  conflates "the lock is stale" with "the index could not be reached"), which
  would make a check declared local and offline answer differently depending on
  the network;
* it writes -- caches, and the lock itself in some modes -- so it could not be
  an observe-allowlisted program and the check could not stay ``pure``;
* it is not available for every ecosystem anyway, so half the check would be
  structural regardless.

So each ecosystem is compared structurally, against the parts of the lock that
record what the manifest asked for:

| target | manifest              | lock                  | compared                                              |
| ------ | --------------------- | --------------------- | ----------------------------------------------------- |
| pypi   | ``pyproject.toml``    | ``uv.lock``           | the project's own package entry: version, ``metadata.requires-dist``, ``metadata.requires-dev`` |
| npm    | ``package.json``      | ``package-lock.json`` | the root entry (``packages[""]``): name, version, and the four dependency maps |
| go     | ``go.mod``            | ``go.sum``            | every required module has a recorded hash             |

uv and npm both record the requirements they resolved FROM, which is exactly
what a staleness comparison needs: a requirement in the manifest that the lock
never saw (or one the lock still carries that the manifest dropped) is a stale
lock, with no resolution required to see it. Go records no such thing -- the
``require`` lines are the resolution -- so the comparison there is the one
consistency go itself guarantees: every required module has a ``go.sum`` entry.

Which ``uv.lock`` is read is :func:`rlsbl.uv_workspace.locate_uv_lock`'s
answer: a uv workspace member has no lock of its own, so its manifest is
resolved by the workspace root's lock and its entry is found under the member's
own path.

Limits, stated rather than hidden
---------------------------------

* A requirement uv resolves from a SOURCE -- a direct reference
  (``name @ file:///...``) or a ``[tool.uv.sources]`` workspace/path/git/url
  entry -- is compared by PRESENCE only. uv records the source in
  ``requires-dist`` and drops the version specifier entirely, so there is
  nothing on the lock's side for the declared constraint to be compared
  against. Adding or removing the source itself is still drift, and is still
  reported.
* A lockfileVersion 1 ``package-lock.json`` records no root requirement map, so
  only presence of each declared dependency is compared, and the outcome says
  so.
* A ``pyproject.toml`` with a dynamic version has no version to compare.
* No ecosystem's absent lockfile is an error here: whether a project must
  commit a lock is not this check's question. An absent lock is a note.
"""

import json
import os
import re
import tomllib

from .dep_floors import (
    _split_requirement,
    normalize_npm_name,
    normalize_pypi_name,
)
from .uv_workspace import locate_uv_lock

PYPI_RELOCK = "uv lock"
NPM_RELOCK = "npm install --package-lock-only"
GO_RELOCK = "go mod tidy"


class DepLockVerdict:
    """Result of comparing every lockfile in one project against its manifest."""

    def __init__(self, *, problems=None, notes=None, skip_reason=None):
        self.problems = list(problems or [])
        self.notes = list(notes or [])
        self.skip_reason = skip_reason

    @property
    def ok(self):
        return not self.problems


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------


def _load_toml(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


#: The comparison operators a PEP 440 clause can start with, longest first so
#: ``===`` is recognized before ``==``.
_OPERATORS = ("===", "~=", "==", "!=", "<=", ">=", "<", ">")

#: One PEP 440 version, split into the parts the canonical spelling reorders.
#: Anything this does not match is left exactly as written.
_PEP440 = re.compile(
    r"^(?:(?P<epoch>\d+)!)?"
    r"(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre_l>alpha|beta|preview|pre|a|b|c|rc)[-_.]?(?P<pre_n>\d+)?)?"
    r"(?:(?:-(?P<post_n1>\d+))"
    r"|(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>\d+)?))?"
    r"(?P<dev>[-_.]?dev[-_.]?(?P<dev_n>\d+)?)?"
    r"(?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?$",
    re.I,
)

#: PEP 440's pre-release spellings and the single canonical letter each one
#: normalizes to.
_PRE_LETTERS = {
    "alpha": "a", "a": "a",
    "beta": "b", "b": "b",
    "c": "rc", "pre": "rc", "preview": "rc", "rc": "rc",
}


def normalize_version(text):
    """A PEP 440 version in the canonical spelling uv writes.

    uv re-serializes every requirement it locks from its PARSED form, so
    ``uv.lock`` carries the canonical spelling of what ``pyproject.toml``
    declared -- verified against uv's own output, which rewrote
    ``>=1.0.0-alpha1`` to ``>=1.0.0a1``, ``>=1.0.0RC1`` to ``>=1.0.0rc1`` and
    ``>=01.02.03`` to ``>=1.2.3``. Comparing the two texts therefore means
    canonicalizing the declared side the same way, or a manifest that spelled a
    version legally but not canonically reads as a lock that predates it.

    Anything this does not recognize -- a wildcard (``1.0.*``), a local
    directory, an unparseable string -- is returned unchanged rather than
    guessed at.
    """
    raw = (text or "").strip()
    m = _PEP440.match(raw)
    if m is None:
        return raw
    out = ""
    if m.group("epoch"):
        out += f"{int(m.group('epoch'))}!"
    out += ".".join(str(int(part)) for part in m.group("release").split("."))
    if m.group("pre_l"):
        letter = _PRE_LETTERS[m.group("pre_l").lower()]
        out += f"{letter}{int(m.group('pre_n') or 0)}"
    if m.group("post_n1") is not None:
        out += f".post{int(m.group('post_n1'))}"
    elif m.group("post_l"):
        out += f".post{int(m.group('post_n2') or 0)}"
    if m.group("dev"):
        out += f".dev{int(m.group('dev_n') or 0)}"
    if m.group("local"):
        out += "+" + re.sub(r"[-_.]", ".", m.group("local").lower())
    return out


def normalize_specifier(text):
    """A PEP 440 / npm specifier reduced to a comparable form.

    Whitespace is dropped, each clause's version is canonicalized (see
    :func:`normalize_version`) and comma-separated clauses are sorted, so
    ``">=1, <2"`` and ``"<2,>=1"`` are the same constraint -- which they are,
    and which is exactly the reordering uv performs when it writes the lock.

    ``===`` is left alone: PEP 440 defines arbitrary equality as a literal
    string match, so canonicalizing its operand would change its meaning.
    """
    clauses = [c.strip() for c in (text or "").replace(" ", "").split(",")]
    return ",".join(sorted(_normalize_clause(c) for c in clauses if c))


def _normalize_clause(clause):
    """One comparison clause with its version canonicalized."""
    for op in _OPERATORS:
        if clause.startswith(op):
            if op == "===":
                return clause
            return op + normalize_version(clause[len(op):])
    return clause


# ---------------------------------------------------------------------------
# pypi: pyproject.toml vs uv.lock
# ---------------------------------------------------------------------------


def _declared_pypi_requirements(data, sourced=frozenset()):
    """``(runtime_specs, dev_specs)`` declared by a parsed ``pyproject.toml``.

    *runtime_specs* is ``{name: {specifier, ...}}`` over ``[project]``'s
    dependencies AND every optional-dependency extra, because uv folds extras
    into ``requires-dist`` with an ``extra ==`` marker. *dev_specs* is
    ``{group: {name: {specifier, ...}}}`` over PEP 735 ``[dependency-groups]``
    plus the legacy ``[tool.uv].dev-dependencies``, which uv records as the
    ``dev`` group.

    A direct reference (``name @ file:///...``) and a requirement redirected by
    ``[tool.uv.sources]`` both contribute their name with the sentinel
    specifier :data:`_URL_SPEC`: the lock records the source, not a specifier,
    so only presence is comparable.
    """
    project = data.get("project") or {}
    runtime = {}
    entries = list(project.get("dependencies") or [])
    for extra_entries in (project.get("optional-dependencies") or {}).values():
        entries.extend(extra_entries or [])
    for entry in entries:
        _add_requirement(runtime, entry, sourced)

    dev = {}
    for group, group_entries in (data.get("dependency-groups") or {}).items():
        bucket = dev.setdefault(group, {})
        for entry in group_entries or []:
            _add_requirement(bucket, entry, sourced)
    legacy = ((data.get("tool") or {}).get("uv") or {}).get("dev-dependencies")
    if isinstance(legacy, list) and legacy:
        bucket = dev.setdefault("dev", {})
        for entry in legacy:
            _add_requirement(bucket, entry, sourced)
    return runtime, dev


#: Stands in for the specifier of a requirement the lock resolves from a
#: SOURCE, on both sides of the comparison, so it is compared by presence only.
_URL_SPEC = "(direct reference)"

#: ``[tool.uv.sources]`` keys that make uv resolve a requirement from a source
#: instead of from a version specifier. Verified against uv's own output: the
#: locked ``requires-dist`` entry then carries the source and NO ``specifier``
#: at all, so the manifest's constraint has nothing on the other side to be
#: compared against. ``index`` is deliberately absent -- it only selects which
#: registry a version is downloaded from, and the specifier survives.
_SOURCE_KEYS = ("workspace", "path", "git", "url")


def uv_sources_table(data):
    """The ``[tool.uv.sources]`` table of a parsed manifest, or ``{}``."""
    sources = ((data or {}).get("tool") or {}).get("uv") or {}
    table = sources.get("sources")
    return table if isinstance(table, dict) else {}


def source_backed_names(*tables):
    """Normalized names whose ``[tool.uv.sources]`` entry erases the specifier.

    Later *tables* override earlier ones, which is uv's own precedence: the
    sources a WORKSPACE ROOT declares apply to every member, unless the member
    declares its own entry for that name. Reading only the member's table
    reported every member of a flat uv workspace -- where the sibling sources
    are declared once at the root -- as a stale lock.

    A source may be declared as a table or as a LIST of marker-gated tables;
    one source-bearing element is enough, because the lock then records the
    source for that requirement.
    """
    merged = {}
    for table in tables:
        merged.update(table or {})
    found = set()
    for name, spec in merged.items():
        if not isinstance(name, str):
            continue
        for entry in (spec if isinstance(spec, list) else [spec]):
            if isinstance(entry, dict) and any(k in entry for k in _SOURCE_KEYS):
                found.add(normalize_pypi_name(name))
                break
    return found


def _inherited_sources(root):
    """The ``[tool.uv.sources]`` a uv workspace root lends to *root*, or ``{}``."""
    from .uv_workspace import find_uv_workspace_root

    workspace_root = find_uv_workspace_root(root)
    if workspace_root is None:
        return {}
    return uv_sources_table(_load_toml(os.path.join(workspace_root, "pyproject.toml")))


def _add_requirement(bucket, entry, sourced=frozenset()):
    """Record one manifest requirement in *bucket* (``{name: {spec, ...}}``)."""
    if not isinstance(entry, str):
        # PEP 735 `{include-group = "..."}` and anything else non-textual: the
        # included group's own entries are compared under their own name.
        return
    split = _split_requirement(entry)
    if split is None:
        return
    name, kind, constraint = split
    if kind == "url" or name in sourced:
        spec = _URL_SPEC
    else:
        spec = normalize_specifier(constraint)
    bucket.setdefault(name, set()).add(spec)


def _locked_pypi_requirements(entries):
    """``{name: {specifier, ...}}`` from a lock's ``requires-dist``-shaped list."""
    result = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        if any(key in entry for key in ("url", "path", "directory", "git", "editable")):
            spec = _URL_SPEC
        else:
            spec = normalize_specifier(entry.get("specifier") or "")
        result.setdefault(normalize_pypi_name(name), set()).add(spec)
    return result


def _project_lock_entry(lock, relpath):
    """The lock's package entry for the project at *relpath*, or None.

    uv records a workspace member (and a standalone project) as an editable or
    virtual source naming its directory relative to the lock, so the entry is
    found by that path rather than by name -- a name in ``pyproject.toml`` that
    the lock has not caught up with is exactly the drift being looked for.
    """
    wanted = relpath.replace(os.sep, "/")
    for package in lock.get("package") or []:
        if not isinstance(package, dict):
            continue
        source = package.get("source")
        if not isinstance(source, dict):
            continue
        for key in ("editable", "virtual"):
            value = source.get(key)
            if isinstance(value, str) and value.rstrip("/") == wanted.rstrip("/"):
                return package
    return None


def _compare_requirement_sets(declared, locked, *, where, problems):
    """Report every name and specifier difference between the two sides."""
    for name in sorted(set(declared) - set(locked)):
        problems.append(
            f"pypi: {where} declares {name}, which uv.lock does not record -- "
            f"the lock predates that requirement. Run `{PYPI_RELOCK}`."
        )
    for name in sorted(set(locked) - set(declared)):
        problems.append(
            f"pypi: uv.lock still records {name} under {where}, which the "
            f"manifest no longer declares -- the lock predates that removal. "
            f"Run `{PYPI_RELOCK}`."
        )
    for name in sorted(set(declared) & set(locked)):
        if declared[name] != locked[name]:
            problems.append(
                f"pypi: {where} constrains {name} as "
                f"{_render_specs(declared[name])}, but uv.lock resolved it "
                f"from {_render_specs(locked[name])} -- the constraint changed "
                f"after the lock was written. Run `{PYPI_RELOCK}`."
            )


def _render_specs(specs):
    rendered = sorted(s or "(no constraint)" for s in specs)
    return " / ".join(rendered)


def _evaluate_pypi(root):
    manifest = os.path.join(root, "pyproject.toml")
    data = _load_toml(manifest)
    if data is None:
        return [], []
    if "project" not in data and "tool" not in data:
        return [], []

    search = locate_uv_lock(root)
    if search.location is None:
        return [], [f"pypi: no uv.lock -- probed {search.probed}"]
    lock = _load_toml(search.location.path)
    if lock is None:
        return [
            f"pypi: {search.location.path} exists but could not be read as "
            f"TOML, so it cannot be compared against pyproject.toml. Run "
            f"`{PYPI_RELOCK}`."
        ], []

    lock_dir = os.path.dirname(search.location.path)
    relpath = os.path.relpath(root, lock_dir)
    entry = _project_lock_entry(lock, relpath)
    if entry is None:
        return [
            f"pypi: {search.location.path} has no package entry for this "
            f"project (expected an editable or virtual source at "
            f"'{relpath.replace(os.sep, '/')}'), so the lock does not resolve "
            f"this manifest at all. Run `{PYPI_RELOCK}`."
        ], []

    problems = []
    declared_version = (data.get("project") or {}).get("version")
    locked_version = entry.get("version")
    if isinstance(declared_version, str) and isinstance(locked_version, str):
        if declared_version != locked_version:
            problems.append(
                f"pypi: pyproject.toml declares version {declared_version} but "
                f"uv.lock records {locked_version} for this project -- the lock "
                f"predates the version change. Run `{PYPI_RELOCK}`."
            )

    # uv omits [package.metadata] entirely for a project that declares no
    # requirement at all, so an ABSENT table is an empty one -- reading it as
    # an unreadable lock made every dependency-free package a hard error
    # naming a relock that could not fix it. A table that is present but not a
    # table is a different thing: a malformed lock, which is still an error.
    metadata = entry.get("metadata", {})
    if not isinstance(metadata, dict):
        problems.append(
            f"pypi: this project's uv.lock entry has a metadata section that "
            f"is not a table, so the requirements it was resolved from cannot "
            f"be compared. Run `{PYPI_RELOCK}`."
        )
        return problems, []

    sourced = source_backed_names(_inherited_sources(root), uv_sources_table(data))
    declared_runtime, declared_dev = _declared_pypi_requirements(data, sourced)
    _compare_requirement_sets(
        declared_runtime,
        _locked_pypi_requirements(metadata.get("requires-dist")),
        where="pyproject.toml",
        problems=problems,
    )

    locked_dev_raw = metadata.get("requires-dev")
    locked_dev = {
        group: _locked_pypi_requirements(entries)
        for group, entries in (locked_dev_raw or {}).items()
    }
    for group in sorted(set(declared_dev) | set(locked_dev)):
        _compare_requirement_sets(
            declared_dev.get(group, {}),
            locked_dev.get(group, {}),
            where=f"dependency group '{group}'",
            problems=problems,
        )

    notes = []
    if not problems:
        notes.append(
            f"pypi: {os.path.relpath(search.location.path, root)} resolves "
            f"pyproject.toml"
        )
    return problems, notes


# ---------------------------------------------------------------------------
# npm: package.json vs package-lock.json
# ---------------------------------------------------------------------------

_NPM_SECTIONS = (
    "dependencies", "devDependencies", "optionalDependencies", "peerDependencies",
)


def _npm_section(doc, section):
    entries = doc.get(section)
    if not isinstance(entries, dict):
        return {}
    return {
        normalize_npm_name(name): rng
        for name, rng in entries.items()
        if isinstance(name, str) and isinstance(rng, str)
    }


def _evaluate_npm(root):
    manifest = _load_json(os.path.join(root, "package.json"))
    if manifest is None:
        return [], []
    lock_path = os.path.join(root, "package-lock.json")
    lock = _load_json(lock_path)
    if lock is None:
        if os.path.isfile(lock_path):
            return [
                f"npm: {lock_path} exists but could not be read as JSON, so it "
                f"cannot be compared against package.json. Run `{NPM_RELOCK}`."
            ], []
        return [], ["npm: no package-lock.json -- nothing to compare"]

    problems = []
    version = lock.get("lockfileVersion")
    root_entry = (lock.get("packages") or {}).get("")
    if isinstance(root_entry, dict):
        for field in ("name", "version"):
            declared = manifest.get(field)
            locked = root_entry.get(field)
            if isinstance(declared, str) and declared != locked:
                problems.append(
                    f"npm: package.json declares {field} {declared!r} but "
                    f"package-lock.json records {locked!r} for the root "
                    f"package -- the lock predates that change. Run "
                    f"`{NPM_RELOCK}`."
                )
        for section in _NPM_SECTIONS:
            declared = _npm_section(manifest, section)
            locked = _npm_section(root_entry, section)
            for name in sorted(set(declared) - set(locked)):
                problems.append(
                    f"npm: package.json declares {name} in {section}, which "
                    f"package-lock.json's root entry does not record -- the "
                    f"lock predates that dependency. Run `{NPM_RELOCK}`."
                )
            for name in sorted(set(locked) - set(declared)):
                problems.append(
                    f"npm: package-lock.json still records {name} in "
                    f"{section}, which package.json no longer declares -- the "
                    f"lock predates that removal. Run `{NPM_RELOCK}`."
                )
            for name in sorted(set(declared) & set(locked)):
                if normalize_specifier(declared[name]) != normalize_specifier(locked[name]):
                    problems.append(
                        f"npm: package.json constrains {name} as "
                        f"{declared[name]!r} in {section} but "
                        f"package-lock.json records {locked[name]!r} -- the "
                        f"range changed after the lock was written. Run "
                        f"`{NPM_RELOCK}`."
                    )
        notes = []
        if not problems:
            notes.append("npm: package-lock.json resolves package.json")
        return problems, notes

    # lockfileVersion 1 records no root requirement map: only presence is
    # comparable, and the note says so rather than implying a full comparison.
    locked_names = set()
    deps = lock.get("dependencies")
    if isinstance(deps, dict):
        locked_names = {normalize_npm_name(name) for name in deps}
    for section in ("dependencies", "optionalDependencies"):
        for name in sorted(set(_npm_section(manifest, section)) - locked_names):
            problems.append(
                f"npm: package.json declares {name} in {section}, which "
                f"package-lock.json does not resolve -- the lock predates that "
                f"dependency. Run `{NPM_RELOCK}`."
            )
    notes = [
        f"npm: lockfileVersion {version} records no root requirement map, so "
        f"only the presence of each declared dependency was compared"
    ]
    return problems, notes


# ---------------------------------------------------------------------------
# go: go.mod vs go.sum
# ---------------------------------------------------------------------------

_GO_REQUIRE_LINE = re.compile(r"^\s*([^\s()]+)\s+(v[^\s]+)")


def _strip_go_comment(line):
    return line.split("//", 1)[0].rstrip()


def parse_go_mod(text):
    """``(requires, replaced)`` from a ``go.mod``'s text.

    *requires* is ``{module: version}`` over every ``require`` line, block form
    and single-line form alike. *replaced* is the set of module paths a
    ``replace`` directive redirects to a filesystem path -- those resolve from
    disk and carry no ``go.sum`` entry.
    """
    requires = {}
    replaced = set()
    block = None
    for raw in text.splitlines():
        line = _strip_go_comment(raw).strip()
        if not line:
            continue
        if block is not None:
            if line == ")":
                block = None
                continue
            if block == "require":
                m = _GO_REQUIRE_LINE.match(line)
                if m:
                    requires[m.group(1)] = m.group(2)
            elif block == "replace":
                _record_replace(line, replaced)
            continue
        if line.startswith("require ("):
            block = "require"
            continue
        if line.startswith("replace ("):
            block = "replace"
            continue
        if line.startswith("require "):
            m = _GO_REQUIRE_LINE.match(line[len("require "):])
            if m:
                requires[m.group(1)] = m.group(2)
            continue
        if line.startswith("replace "):
            _record_replace(line[len("replace "):], replaced)
    return requires, replaced


def _record_replace(line, replaced):
    """Record a ``a [vX] => target`` line when its target is a local path."""
    if "=>" not in line:
        return
    left, right = line.split("=>", 1)
    module = left.strip().split()[0] if left.strip() else ""
    target = right.strip().split()[0] if right.strip() else ""
    if not module or not target:
        return
    if target.startswith((".", "/")) or (len(target) > 1 and target[1] == ":"):
        replaced.add(module)


def _go_sum_keys(path):
    """``{(module, version)}`` recorded by a ``go.sum``-format file."""
    keys = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    keys.add((parts[0], parts[1].removesuffix("/go.mod")))
    except (OSError, UnicodeDecodeError):
        return None
    return keys


def _find_go_work_sum(root):
    """A ``go.work.sum`` of the go workspace above *root*, or None."""
    current = os.path.realpath(root)
    while True:
        if os.path.isfile(os.path.join(current, "go.work")):
            candidate = os.path.join(current, "go.work.sum")
            return candidate if os.path.isfile(candidate) else None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _evaluate_go(root):
    mod_path = os.path.join(root, "go.mod")
    if not os.path.isfile(mod_path):
        return [], []
    try:
        with open(mod_path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return [f"go: {mod_path} could not be read"], []

    requires, replaced = parse_go_mod(text)
    wanted = {
        (module, version)
        for module, version in requires.items()
        if module not in replaced
    }
    if not wanted:
        return [], ["go: go.mod requires no external module -- no sums owed"]

    sum_path = os.path.join(root, "go.sum")
    keys = _go_sum_keys(sum_path) if os.path.isfile(sum_path) else None
    if keys is None and not os.path.isfile(sum_path):
        return [
            f"go: go.mod requires {len(wanted)} module(s) but there is no "
            f"go.sum, so nothing verifies what a build downloads. Run "
            f"`{GO_RELOCK}`."
        ], []
    if keys is None:
        return [f"go: {sum_path} could not be read"], []

    work_sum = _find_go_work_sum(root)
    if work_sum is not None:
        extra = _go_sum_keys(work_sum)
        if extra:
            keys |= extra

    missing = sorted(f"{module} {version}" for module, version in wanted - keys)
    if missing:
        shown = ", ".join(missing[:5])
        if len(missing) > 5:
            shown += f", and {len(missing) - 5} more"
        return [
            f"go: {len(missing)} required module(s) have no go.sum entry "
            f"({shown}), so go.sum no longer covers go.mod. Run `{GO_RELOCK}`."
        ], []
    return [], [f"go: go.sum covers all {len(wanted)} required module(s)"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def evaluate_dep_locks(project_root):
    """Compare every lockfile in *project_root* against its manifest.

    Returns a :class:`DepLockVerdict`. A project with no readable manifest in
    any of the three ecosystems comes back with a skip reason.
    """
    root = str(project_root)
    problems = []
    notes = []
    for evaluate in (_evaluate_pypi, _evaluate_npm, _evaluate_go):
        found, ecosystem_notes = evaluate(root)
        problems.extend(found)
        notes.extend(ecosystem_notes)
    if not problems and not notes:
        return DepLockVerdict(
            skip_reason="no pyproject.toml, package.json or go.mod to compare",
        )
    return DepLockVerdict(problems=problems, notes=notes)
