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
"""

import re
from pathlib import Path

import pytest

from conftest import make_workspace
from rlsbl.errors import WorkspaceError
from rlsbl.workspace import WorkspaceProject, load_workspace, save_workspace


SOURCE_ROOT = Path(__file__).resolve().parent.parent / "rlsbl"

# Reads of a member key named ``watch``: attribute access on a project, a dict
# subscript, a ``.get()``, or a membership test. Deliberately not the bare word.
_KEY_READ = re.compile(
    r"""(?:
        \.watch\b                      # proj.watch
      | \[\s*["']watch["']\s*\]        # proj["watch"]
      | \.get\(\s*["']watch["']        # proj.get("watch")
      | ["']watch["']\s+in\b           # "watch" in proj
    )""",
    re.VERBOSE,
)

# ``flags["watch"]`` and friends are the release command's --watch flag, a
# different thing that happens to share a word.
_RELEASE_FLAG = re.compile(r"\bflags?\b")

# ``rlsbl.commands.watch`` is the CI-watching module; ``.watch`` there is a
# module name, not a member key.
_MODULE_REF = re.compile(r"\bimport\b|commands\.watch|\.\.watch|\.watch\.")

# The loader's refusal is the ONE place that may name the key. It has to: its
# whole job is telling an operator which line to delete.
_REFUSAL_SITE = "workspace.py"


def _key_reads():
    """Every ``watch``-as-member-key read in the package, with its location."""
    found = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not _KEY_READ.search(line):
                continue
            if _RELEASE_FLAG.search(line) or _MODULE_REF.search(line):
                continue
            found.append((path.relative_to(SOURCE_ROOT.parent), lineno, line.strip()))
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


class TestTheKeyIsRefusedEndToEnd:

    def test_the_loader_refuses_a_workspace_carrying_it(self, tmp_path):
        make_workspace(str(tmp_path), [{"path": "pkg", "name": "pkg"}])
        projects = load_workspace(str(tmp_path))
        for proj in projects:
            if proj["name"] == "pkg":
                proj["watch"] = ["shared/**"]
        save_workspace(str(tmp_path), projects)

        with pytest.raises(WorkspaceError, match="'watch' key is no longer supported"):
            load_workspace(str(tmp_path))

    def test_the_refusal_names_the_member_and_the_way_out(self, tmp_path):
        make_workspace(str(tmp_path), [{"path": "pkg", "name": "pkg"}])
        projects = load_workspace(str(tmp_path))
        for proj in projects:
            if proj["name"] == "pkg":
                proj["watch"] = ["shared/**"]
        save_workspace(str(tmp_path), projects)

        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_path))
        message = str(exc.value)
        assert "pkg" in message
        assert "watch" in message

    def test_the_add_command_declares_no_such_flag(self):
        import rlsbl

        add_cmd = dict(rlsbl.app._collect_all_commands())["monorepo.add"]
        assert "watch" not in {flag.name for flag in add_cmd.flags}
