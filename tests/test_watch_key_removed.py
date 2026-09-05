"""The workspace ``watch`` key has exactly one mention left, and it is a refusal.

Territory used to have two spellings: a member's declared ``path``, and a list
of extra globs under ``watch``.  Two spellings meant two members could claim
one file, which is what made single-owner attribution impossible.  The key is
gone: a member owns its declared path and nothing else, the root member owns
the residual, and the CI router's paths filters are derived from that plus the
dependency graph (:mod:`rlsbl.router_filters`).

The word "watch" itself survives legitimately and must not be pinned: the
``rlsbl watch`` command, the ``--watch``/``--no-watch`` release flag and its
``flags["watch"]`` reads, the loader's own refusal error (which has to name
the key it refuses), and the historical changelog files.  What this pins is
narrower and exact: **nothing reads ``watch`` as a workspace member key.**

**What the detector sees, and what it cannot.**  It matches the literal ways a
key is read off an object -- attribute access, subscript, ``.get``, ``.pop``,
``getattr``, membership -- over the whole file text, so a call split across
lines is matched like any other.  Suppression is by exact spelling of the
legitimate uses (``flags["watch"]``, ``flags.get("watch")``, ``--watch``,
``--no-watch``, a ``name="watch"`` flag registration, module references),
never by the mere presence of the word "flag" on the line: a suppression that
broad would hide a real member-key read that happened to sit next to one.

Two blind spots remain, and they are honest ones:

- **an indirect key** -- ``key = "watch"`` followed by ``proj[key]``, or a key
  built by concatenation. Nothing here can see through the variable.
- **exotic dynamic access** -- ``vars(proj)["watch"]``, ``__getattribute__``,
  a key read out of a config-driven table.

Both are visible to :class:`TestTheKeyIsRefusedEndToEnd` below only if they
reach the loader, which refuses the key outright; the detector's job is to
keep the ordinary spellings from creeping back in.
"""

import re
from pathlib import Path

import pytest

from conftest import make_workspace
from rlsbl.errors import WorkspaceError
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    WorkspaceProject,
    load_workspace,
    save_workspace,
)


SOURCE_ROOT = Path(__file__).resolve().parent.parent / "rlsbl"

# Reads of a member key named ``watch``: attribute access on a project, a dict
# subscript, a ``.get()``, a ``.pop()``, a ``getattr()``, or a membership test.
# Deliberately not the bare word. ``\s`` spans newlines, so a call broken
# across lines matches exactly like a single-line one.
_KEY_READ = re.compile(
    r"""(?:
        \.watch\b                              # proj.watch
      | \[\s*["']watch["']\s*\]                # proj["watch"]
      | \.get\(\s*["']watch["']                # proj.get("watch")
      | \.pop\(\s*["']watch["']                # proj.pop("watch")
      | getattr\(\s*[^,()]+,\s*["']watch["']   # getattr(proj, "watch", [])
      | ["']watch["']\s+in\b                   # "watch" in proj
    )""",
    re.VERBOSE,
)

# The release command's --watch flag: a different thing that happens to share
# a word. Named by its exact spellings rather than by the presence of "flag"
# anywhere on the line, which used to suppress whole statements wholesale.
_RELEASE_FLAG = re.compile(
    r"""(?:
        \bflags?\.(?:get|pop)\(\s*["']watch["']   # flags.get("watch")
      | \bflags?\[\s*["']watch["']\s*\]           # flags["watch"]
      | --no-watch
      | --watch
      | name\s*=\s*["']watch["']                  # strictcli flag registration
    )""",
    re.VERBOSE,
)

# ``rlsbl.commands.watch`` is the CI-watching module; ``.watch`` there is a
# module name, not a member key.
_MODULE_REF = re.compile(r"\bimport\b|commands\.watch|\.\.watch|\.watch\.")

# The loader's refusal is the ONE place that may name the key. It has to: its
# whole job is telling an operator which line to delete.
_REFUSAL_SITE = "workspace.py"


def _key_reads(source_root=SOURCE_ROOT):
    """Every ``watch``-as-member-key read under *source_root*, with location."""
    found = []
    for path in sorted(Path(source_root).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _KEY_READ.finditer(text):
            # The statement the match sits in: from the start of its first
            # line to the end of its last. A multi-line call is judged whole,
            # so `flags.get(\n "watch")` is recognized as the release flag.
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            statement = text[start:] if end == -1 else text[start:end]
            if _RELEASE_FLAG.search(statement) or _MODULE_REF.search(statement):
                continue
            lineno = text.count("\n", 0, match.start()) + 1
            found.append((
                path.relative_to(Path(source_root).parent),
                lineno,
                statement.strip(),
            ))
    return found


class TestNothingReadsTheKey:

    def test_only_the_loader_refusal_names_it(self):
        offenders = [
            (path, lineno, line)
            for path, lineno, line in _key_reads()
            if path.name != _REFUSAL_SITE
        ]
        assert not offenders, (
            "these read `watch` as a workspace member key; the key is gone and "
            "territory comes from the declared path alone: "
            + "; ".join(f"{p}:{n}: {t}" for p, n, t in offenders)
        )

    def test_the_refusal_itself_is_still_there(self):
        """Removing the refusal would make a stale workspace silently wrong."""
        reads = [p.name for p, _n, _t in _key_reads()]
        assert _REFUSAL_SITE in reads, (
            "the loader no longer looks for the `watch` key, so a workspace "
            "still carrying one would load and its extra globs would be "
            "silently ignored"
        )

    def test_the_typed_project_has_no_accessor(self):
        proj = WorkspaceProject({"name": "core", "path": "packages/core"})
        assert not hasattr(proj, "watch")


class TestTheDetectorSeesTheSpellingsItClaims:
    """The pin is only worth what its detector catches.

    Every form below was planted in a throwaway tree rather than in the
    package: planting in the package would make the suite red for everyone
    else sharing the checkout.
    """

    @staticmethod
    def _plant(tmp_path, source):
        (tmp_path / "planted.py").write_text(source, encoding="utf-8")
        return _key_reads(tmp_path)

    @pytest.mark.parametrize("source", [
        "value = proj.watch\n",
        'value = proj["watch"]\n',
        'value = proj.get("watch")\n',
        'value = proj.get(\n    "watch",\n    [],\n)\n',
        'value = proj.pop("watch", None)\n',
        'value = getattr(proj, "watch", [])\n',
        'value = getattr(\n    proj,\n    "watch",\n    [],\n)\n',
        'if "watch" in proj:\n    pass\n',
    ])
    def test_a_member_key_read_is_found(self, tmp_path, source):
        assert self._plant(tmp_path, source), source

    @pytest.mark.parametrize("source", [
        'if flags.get("watch"):\n    pass\n',
        'watch = flags.get("watch", False)\n',
        'value = flags["watch"]\n',
        'value = flags.get(\n    "watch",\n    False,\n)\n',
        'help_text = "pass --watch or --no-watch"\n',
        'strictcli.flag(name="watch", help="Watch CI")\n',
        "from .commands import watch\n",
        "run = commands.watch.poll()\n",
    ])
    def test_a_legitimate_use_is_not_reported(self, tmp_path, source):
        assert self._plant(tmp_path, source) == [], source


class TestTheKeyIsRefusedEndToEnd:

    @staticmethod
    def _plant_the_key(tmp_path):
        """Write a workspace.toml carrying the key.

        Hand-written rather than saved: ``save_workspace`` refuses to write a
        key outside the declared member surface, so the key can only reach the
        file the way a stale repository already has it -- as text.
        """
        make_workspace(str(tmp_path), [{"path": "pkg", "name": "pkg"}])
        ws_file = tmp_path / WORKSPACE_DIR / WORKSPACE_FILE
        text = ws_file.read_text()
        member = 'path = "pkg"\nname = "pkg"'
        assert member in text, text
        text = text.replace(
            member, member + '\nwatch = ["shared/**"]', 1,
        )
        ws_file.write_text(text)

    def test_the_writer_refuses_to_write_it(self, tmp_path):
        make_workspace(str(tmp_path), [{"path": "pkg", "name": "pkg"}])
        projects = load_workspace(str(tmp_path))
        for proj in projects:
            if proj["name"] == "pkg":
                proj["watch"] = ["shared/**"]

        with pytest.raises(WorkspaceError, match="watch"):
            save_workspace(str(tmp_path), projects)

    def test_the_loader_refuses_a_workspace_carrying_it(self, tmp_path):
        self._plant_the_key(tmp_path)

        with pytest.raises(WorkspaceError, match="'watch' key is no longer supported"):
            load_workspace(str(tmp_path))

    def test_the_refusal_names_the_member_and_the_way_out(self, tmp_path):
        self._plant_the_key(tmp_path)

        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_path))
        message = str(exc.value)
        assert "pkg" in message
        assert "watch" in message

    def test_the_add_command_declares_no_such_flag(self):
        import rlsbl

        add_cmd = dict(rlsbl.app._collect_all_commands())["monorepo.add"]
        assert "watch" not in {flag.name for flag in add_cmd.flags}
