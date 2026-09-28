"""A repository's entries for rlsbl's options, and the values they give rlsbl.

Every rlsbl check is an option, ``rlsbl:<check name>``, declared in the
registry rlsbl ships (:mod:`rlsbl.options_registry`). A repository deviates
from an option's default only by filing an entry in
``.strictmetadata/options/`` at its git root; strictspec validates the entries'
shape and every rule of the options model, and rlsbl adds the rule of its own
scope kind: a ``path`` scope names a workspace member's directory relative to
the git root.

Loading refuses on any diagnostic -- a document strictspec cannot read, an
entry of rlsbl's namespace strictspec refuses, or a scope naming no member --
so every check run and every release stops on invalid entries rather than
running with some of them.

Options that stand for an adoption (:data:`ADOPTION_SETTINGS`) default to off
and govern a setting in ``.rlsbl/config.json``: the setting is required while
the option is on, and refused while it is off, since nothing reads it then.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import strictspec
import tomlkit

from . import effects
from .errors import ConfigError
from .options_registry import ADOPTION_OPTIONS, PATH_SCOPE, REGISTRY_TOML

#: The tool name of rlsbl's options namespace.
TOOL = "rlsbl"
PREFIX = TOOL + ":"
#: The options directory, relative to the repository's git root.
OPTIONS_DIR = strictspec.OPTIONS_DIR
#: The options directory's ownership manifest and the owner it names.
MANIFEST_FILE = "manifest.toml"
MANIFEST_OWNER = "strictspec"
_MANIFEST_CONTENT = f'owner = "{MANIFEST_OWNER}"\n'
#: A new subject document opens with the options-entries format version.
_ENTRIES_HEADER = "format_version = 1\n"
OFF = "off"

#: The config setting each adoption option governs, as its key path in
#: ``.rlsbl/config.json``, and what a declaration of it holds.
ADOPTION_SETTINGS = {
    "dep-floors": (("internal_dep_floors",), "a list of the ecosystem-internal package names whose floors are policed"),
    "format": (("checks", "format"), 'a block with the "paths" to check'),
    "lint": (("checks", "lint"), 'a block with the "paths" to check'),
    "strictspec-certificate-gate": (("strictspec_gate",), 'a block naming the "certificate" to judge'),
    "test-sandbox": (("test_sandbox",), "the sandboxed test runner's settings"),
    "type-check": (("checks", "type-check"), 'a block with the "paths" to check'),
}
assert set(ADOPTION_SETTINGS) == set(ADOPTION_OPTIONS)


class OptionsError(ConfigError):
    """A repository's options that rlsbl refuses, or an entry it will not write."""


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

_registry_cache: list = []


def registry_text() -> str:
    """The shipped registry document, byte for byte."""
    return REGISTRY_TOML.read_text(encoding="utf-8")


def checked_registry() -> strictspec.CheckedOptionsRegistry:
    """The shipped registry, validated by strictspec (once per process).

    A shipped registry strictspec refuses is an rlsbl defect, never a
    repository's condition, so it raises RuntimeError.
    """
    if not _registry_cache:
        reg, diags = strictspec.read_options_registry(registry_text().encode())
        if reg is not None:
            checked, diags = strictspec.validate_options_registry(reg)
        if diags:
            detail = "; ".join(f"{d.code} at {d.path}: {d.message}" for d in diags)
            raise RuntimeError(f"rlsbl's shipped options registry is invalid: {detail}")
        _registry_cache.append(checked)
    return _registry_cache[0]


def declaration(name):
    """The declaration of rlsbl's option *name*, or None."""
    option = checked_registry().option(name)
    return None if option is None else option.declaration


def strongest_value(name):
    """The strongest value option *name* ranks (``error``, or ``on``)."""
    return checked_registry().option(name).ranking.values[0]


# ---------------------------------------------------------------------------
# Locating a repository and its entries
# ---------------------------------------------------------------------------


def repository_root(start):
    """The git work-tree root enclosing *start*, or None outside a repository.

    Found the way git finds it: the nearest directory, *start* included,
    holding a ``.git`` entry (a directory, or the file a linked worktree has).
    """
    current = os.path.realpath(str(start))
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def scope_of(project_dir, root):
    """The path scope naming *project_dir*: its directory relative to *root*,
    POSIX-spelled, ``.`` for the root itself.
    """
    rel = os.path.relpath(os.path.realpath(str(project_dir)), root)
    return PurePosixPath(*Path(rel).parts).as_posix() if rel != "." else "."


def _workspace_scopes(project_dir, root):
    """The path scopes of the rlsbl workspace *project_dir* belongs to, or None
    when it belongs to none inside this repository.
    """
    from .workspace import find_workspace_root, load_workspace

    ws_root = find_workspace_root(str(project_dir))
    if ws_root is None:
        return None
    ws_root = os.path.realpath(ws_root)
    if os.path.relpath(ws_root, root).startswith(".."):
        return None
    return {
        scope_of(os.path.join(ws_root, project["path"]), root)
        for project in load_workspace(ws_root)
    }


def _entry_source(entry):
    where = f"{entry.id} in {OPTIONS_DIR}/{entry.file}"
    if entry.scope is not None:
        where += f" (scope {entry.scope!r})"
    return where


def _diagnostic_lines(loaded, entries, root, project_dir):
    """Every refusal of the loaded documents and of rlsbl's entries, each a
    line of text naming the document it concerns.
    """
    lines = []
    for invalid in loaded.invalid:
        for d in invalid.diagnostics:
            lines.append(f"{OPTIONS_DIR}/{invalid.file}: {d.code}: {d.message}")
    _accepted, diags = strictspec.validate_options_namespace(
        TOOL, checked_registry(), entries,
    )
    lines.extend(f"{d.code}: {d.message}" for d in diags)
    scoped = [
        e for e in entries
        if e.id.startswith(PREFIX) and e.scope is not None
        and (decl := declaration(e.id[len(PREFIX):])) is not None
        and decl.scope == PATH_SCOPE
    ]
    if scoped:
        scopes = _workspace_scopes(project_dir, root)
        for e in scoped:
            at = f"{OPTIONS_DIR}/{e.file}: entry[{e.index}]"
            if scopes is None:
                lines.append(
                    f"{at}: {e.id} is scoped to {e.scope!r}, but this project "
                    f"belongs to no rlsbl workspace, so there is no member for a "
                    f"path scope to name. Remove the entry's scope."
                )
            elif e.scope not in scopes:
                lines.append(
                    f"{at}: {e.id} is scoped to {e.scope!r}, which names no "
                    f"member of this workspace. A path scope is a member's "
                    f"directory relative to the repository root: "
                    f"{', '.join(sorted(scopes))}."
                )
    return lines


@dataclass
class RepositoryOptions:
    """The accepted entries of rlsbl's namespace in one repository."""

    root: str | None
    entries: tuple = field(default_factory=tuple)

    def value(self, name, project_dir):
        """The value option *name* has for the project at *project_dir*.

        An entry scoped to the project's directory wins over an entry with no
        scope; without either, the option runs at its default.
        """
        decl = declaration(name)
        if decl is None:
            raise KeyError(f"{PREFIX}{name} is not an option rlsbl declares")
        chosen = None
        if self.root is not None:
            here = scope_of(project_dir, self.root)
            for entry in self.entries:
                if entry.id != PREFIX + name:
                    continue
                if entry.scope is None:
                    chosen = chosen or entry
                elif decl.scope == PATH_SCOPE and entry.scope == here:
                    chosen = entry
                    break
        if chosen is None:
            return OptionValue(decl.default, f"{PREFIX}{name} default", None)
        return OptionValue(chosen.current, _entry_source(chosen), chosen)


@dataclass(frozen=True)
class OptionValue:
    """An option's value for one project and where it came from."""

    value: str
    source: str
    entry: object


_load_cache: dict = {}


def _snapshot(root, project_dir):
    """The bytes every judgment of *root*'s options depends on."""
    from .workspace import find_workspace_root

    directory = os.path.join(root, OPTIONS_DIR)
    files = ()
    if os.path.isdir(directory):
        files = tuple(
            (name, Path(directory, name).read_bytes())
            for name in sorted(os.listdir(directory))
            if name.endswith(".toml") and os.path.isfile(os.path.join(directory, name))
        )
    ws_root = find_workspace_root(str(project_dir))
    workspace = None
    if ws_root is not None:
        ws_file = os.path.join(ws_root, ".rlsbl-monorepo", "workspace.toml")
        workspace = (ws_root, Path(ws_file).read_bytes())
    return (root, files, workspace)


def load(project_dir):
    """rlsbl's options for the repository enclosing *project_dir*.

    Outside a git repository there is no options directory, so every option
    is at its default. Raises :class:`OptionsError` naming every refusal.
    """
    root = repository_root(project_dir)
    if root is None:
        return RepositoryOptions(None)
    try:
        key = _snapshot(root, project_dir)
    except OSError as e:
        raise OptionsError(f"cannot read {os.path.join(root, OPTIONS_DIR)}: {e}") from e
    cached = _load_cache.get(key)
    if cached is not None:
        return cached
    try:
        loaded = strictspec.load_options_entries(root)
    except OSError as e:
        raise OptionsError(f"cannot read {os.path.join(root, OPTIONS_DIR)}: {e}") from e
    lines = _diagnostic_lines(loaded, loaded.entries, root, project_dir)
    if lines:
        raise OptionsError(
            f"rlsbl refuses the options in {os.path.join(root, OPTIONS_DIR)}, "
            f"so it runs nothing with them. Fix each entry named below "
            f"(`rlsbl options set` rewrites an rlsbl entry; a hand edit is "
            f"validated the same way):\n"
            + "\n".join(f"  {line}" for line in lines)
        )
    result = RepositoryOptions(
        root, tuple(e for e in loaded.entries if e.id.startswith(PREFIX)),
    )
    if len(_load_cache) > 64:
        _load_cache.clear()
    _load_cache[key] = result
    return result


def option_value(name, project_dir):
    """The :class:`OptionValue` option *name* has for *project_dir*."""
    return load(project_dir).value(name, project_dir)


def is_off(name, project_dir):
    return option_value(name, project_dir).value == OFF


# ---------------------------------------------------------------------------
# Check values
# ---------------------------------------------------------------------------


def check_value(name, project_dir):
    """The strictcli check value option *name* gives its check, or None to run
    it at its registered severity.

    A check that is not an rlsbl option (a config-declared external check) is
    left alone. An adoption option with no entry is off, by default.
    """
    import strictcli

    decl = declaration(name)
    if decl is None:
        return None
    value = option_value(name, project_dir)
    if value.entry is None and value.value != OFF:
        return None
    return strictcli.CheckValue(value=value.value, source=value.source)


# ---------------------------------------------------------------------------
# Adoption settings
# ---------------------------------------------------------------------------


def _has_setting(config, path):
    node = config or {}
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def set_command(name, current, ideal, reason, scope=None):
    """The ``rlsbl options set`` invocation writing one entry."""
    command = f"rlsbl options set {PREFIX}{name} --current {current} --ideal {ideal}"
    if scope is not None:
        command += f" --scope {scope}"
    return command + f' --reason "{reason}"'


def member_scope(project_dir):
    """The path scope an entry for *project_dir* alone carries: its directory
    relative to the git root when it is a member of an rlsbl workspace, or
    None when it belongs to none (an entry without a scope covers it, the
    repository's one project).
    """
    root = repository_root(project_dir)
    if root is None or _workspace_scopes(project_dir, root) is None:
        return None
    return scope_of(project_dir, root)


def setting_problem(name, config, project_dir):
    """The disagreement between adoption option *name* and the setting it
    governs, as a message naming both fixes, or None when they agree: the
    setting is present exactly while the option is on.
    """
    path, holds = ADOPTION_SETTINGS[name]
    value = option_value(name, project_dir)
    setting = ".".join(path)
    present = _has_setting(config, path)
    if value.value == OFF and present:
        strongest = strongest_value(name)
        return (
            f"{setting} is set in .rlsbl/config.json, but {PREFIX}{name} is "
            f"off ({value.source}), so nothing reads it. Delete {setting} "
            f"from .rlsbl/config.json, or switch the option on: "
            + set_command(
                name, strongest, strongest,
                f"<why this project adopts {name}>",
                scope=member_scope(project_dir),
            )
        )
    if value.value != OFF and not present:
        return (
            f"{PREFIX}{name} is {value.value} ({value.source}), but "
            f".rlsbl/config.json declares no {setting}. Declare {setting} "
            f"({holds}), or switch the option off by deleting its entry "
            f"from {OPTIONS_DIR}/{value.entry.file}."
        )
    return None


def settings_problems(config, project_dir):
    """Every adoption setting that disagrees with its option: set while the
    option is off (nothing reads it), or absent while the option is on.
    """
    problems = []
    for name in sorted(ADOPTION_SETTINGS):
        problem = setting_problem(name, config, project_dir)
        if problem is not None:
            problems.append(problem)
    return problems


# ---------------------------------------------------------------------------
# Writing an entry
# ---------------------------------------------------------------------------


@dataclass
class SetResult:
    id: str
    file: str
    scope: str | None
    current: str
    ideal: str
    reason: str
    action: str = ""
    class_: str = ""
    written: list = field(default_factory=list)


def _namespace_refusal(option_id):
    tool, sep, _name = option_id.partition(":")
    if not sep:
        return (
            f"{option_id!r} is not an option id: an id is <tool>:<name>, and "
            f"rlsbl writes its own, {PREFIX}<name>. `rlsbl options registry` "
            f"lists every name."
        )
    return (
        f"{option_id!r} is an option of {tool}, not of rlsbl: rlsbl writes only "
        f"its own entries, {PREFIX}<name>. {tool}'s entries are written by "
        f"{tool}'s own command, or by hand."
    )


def _manifest_missing(root):
    """Whether the options directory's manifest is absent; refuses one that
    names another owner or is not a manifest at all.
    """
    path = os.path.join(root, OPTIONS_DIR, MANIFEST_FILE)
    if not os.path.exists(path):
        return True
    relative = f"{OPTIONS_DIR}/{MANIFEST_FILE}"
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise OptionsError(f"cannot read {relative}: {e}") from e
    if data != {"owner": MANIFEST_OWNER}:
        raise OptionsError(
            f"{relative} must name {MANIFEST_OWNER!r} as the owner of "
            f"{OPTIONS_DIR}/, and nothing else: several tools share that "
            f"directory, and strictspec holds its schemas. It holds "
            f"{data!r}; write this line instead:\n{_MANIFEST_CONTENT.rstrip()}"
        )
    return False


def _render(path, index, option_id, scope, current, ideal, reason):
    """The subject document with the entry written: entry *index* updated in
    place, or a new entry appended when *index* is None. Every other line is
    kept as it stands.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = _ENTRIES_HEADER
    document = tomlkit.parse(raw)
    if index is not None:
        entry = document["entry"][index]
        entry["current"] = current
        entry["ideal"] = ideal
        entry["reason"] = reason
        return tomlkit.dumps(document)
    table = tomlkit.table()
    table.add("id", option_id)
    if scope is not None:
        table.add("scope", scope)
    table.add("current", current)
    table.add("ideal", ideal)
    table.add("reason", reason)
    if "entry" not in document:
        document.append("entry", tomlkit.aot())
    document["entry"].append(table)
    text = tomlkit.dumps(document)
    return _blank_line_before_tables(text)


def _blank_line_before_tables(text):
    """One blank line before every ``[[entry]]`` written flush against what
    precedes it, so an entry the command wrote looks like one written by hand.
    """
    out = []
    for line in text.rstrip("\n").split("\n"):
        if line.startswith("[[") and out and out[-1].strip():
            out.append("")
        out.append(line)
    return "\n".join(out) + "\n"


def set_entry(project_dir, option_id, current, ideal, reason, scope=None):
    """Write one rlsbl entry into the repository's options directory, or update
    the entry already there for the same option and scope.

    strictspec validates the result -- every document's shape and every rlsbl
    entry with this one in place -- before anything is written, and so does
    rlsbl's path-scope rule. The directory and its manifest, naming
    strictspec, are created when absent. Writes go through the effects module,
    so a dry run records them instead.
    """
    if not option_id.startswith(PREFIX):
        raise OptionsError(_namespace_refusal(option_id))
    root = repository_root(project_dir)
    if root is None:
        raise OptionsError(
            f"{project_dir} is not inside a git repository, and a repository's "
            f"options live in {OPTIONS_DIR}/ at its git root."
        )
    manifest_missing = _manifest_missing(root)
    try:
        loaded = strictspec.load_options_entries(root)
    except OSError as e:
        raise OptionsError(f"cannot read {os.path.join(root, OPTIONS_DIR)}: {e}") from e
    if loaded.invalid:
        detail = "\n".join(
            f"  {OPTIONS_DIR}/{invalid.file}: {d.code}: {d.message}"
            for invalid in loaded.invalid for d in invalid.diagnostics
        )
        raise OptionsError(
            f"rlsbl will not edit {OPTIONS_DIR}/ while a document in it is not a "
            f"valid options-entries document. Fix what each diagnostic names:\n"
            f"{detail}"
        )

    decl = declaration(option_id[len(PREFIX):])
    subject_file = (decl.subject if decl is not None else "project") + ".toml"
    candidates = []
    existing = None
    unchanged = False
    in_subject = 0
    for entry in loaded.entries:
        if entry.file == subject_file:
            in_subject += 1
        if (
            existing is None and entry.file == subject_file
            and entry.id == option_id and entry.scope == scope
        ):
            existing = entry.index
            unchanged = (entry.current, entry.ideal, entry.reason) == (current, ideal, reason)
            entry = strictspec.OptionsEntry(
                file=entry.file, index=entry.index, id=entry.id, scope=entry.scope,
                current=current, ideal=ideal, reason=reason,
            )
        candidates.append(entry)
    if existing is None:
        candidates.append(strictspec.OptionsEntry(
            file=subject_file, index=in_subject, id=option_id, scope=scope,
            current=current, ideal=ideal, reason=reason,
        ))
    lines = _diagnostic_lines(loaded, tuple(candidates), root, project_dir)
    if lines:
        raise OptionsError(
            f"{OPTIONS_DIR}/ as it would stand with this entry is refused, so "
            f"nothing was written:\n"
            + "\n".join(f"  {line}" for line in lines)
        )
    accepted, _ = strictspec.validate_options_namespace(TOOL, checked_registry(), candidates)
    result = SetResult(
        id=option_id, file=f"{OPTIONS_DIR}/{subject_file}", scope=scope,
        current=current, ideal=ideal, reason=reason,
    )
    for a in accepted:
        if a.entry.file == subject_file and a.entry.id == option_id and a.entry.scope == scope:
            result.class_ = a.class_
    if unchanged:
        result.action = "unchanged"
        return result

    path = os.path.join(root, OPTIONS_DIR, subject_file)
    content = _render(path, existing, option_id, scope, current, ideal, reason)
    _entries, shape = strictspec.read_options_entries(subject_file, content.encode())
    if shape:
        raise OptionsError(
            f"the rendered {result.file} is not a valid options-entries document: "
            f"{shape[0].code}: {shape[0].message}"
        )
    effects.makedirs(os.path.join(root, OPTIONS_DIR), exist_ok=True)
    if manifest_missing:
        effects.atomic_write_text(os.path.join(root, OPTIONS_DIR, MANIFEST_FILE), _MANIFEST_CONTENT)
        result.written.append(f"{OPTIONS_DIR}/{MANIFEST_FILE}")
    effects.atomic_write_text(path, content, preserve_mode=os.path.exists(path))
    result.written.append(result.file)
    result.action = "updated" if existing is not None else "created"
    _load_cache.clear()
    return result
