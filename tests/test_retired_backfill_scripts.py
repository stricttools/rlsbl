"""The two retired backfill scripts are removal stubs, and nothing calls them.

``scripts/backfill_release_anchors.py`` and
``scripts/set_archived_descriptions.py`` were folded into ``rlsbl release
backfill``. Each file stays, so a saved command line or an older document names
something that says where the work went -- and each does nothing but exit 1
with the command that replaced it.

The last test is the one that keeps the retirement real: no live reference to
either script anywhere in the tree except the stubs, this file, and the
historical changelog record (a released version's notes are immutable, and the
archives materialized by the old script carry its name in a comment because
that IS what wrote them).
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

STUBS = {
    "backfill_release_anchors": "rlsbl release backfill",
    "set_archived_descriptions": "--overrides",
}


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name,names_the_replacement", sorted(STUBS.items()))
class TestTheStubs:

    def test_main_exits_one_with_instructions(self, name, names_the_replacement,
                                              capsys):
        module = load(name)
        assert module.main([]) == 1
        err = capsys.readouterr().err
        assert "was removed" in err
        assert names_the_replacement in err

    def test_running_it_exits_one(self, name, names_the_replacement):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / f"{name}.py")],
            capture_output=True, text=True,
        )
        assert result.returncode == 1
        assert "rlsbl release backfill" in result.stderr

    def test_it_carries_no_implementation(self, name, names_the_replacement):
        """A stub that still imports rlsbl's writers is not a stub."""
        source = (SCRIPTS / f"{name}.py").read_text(encoding="utf-8")
        assert "from rlsbl" not in source
        assert "import tomlkit" not in source


# Where the retired names are allowed to survive: the stubs themselves, this
# file, the generated changelog record of the releases that shipped them, and
# the archives the old script materialized (whose header comment records what
# actually wrote them).
ALLOWED = {
    "scripts/backfill_release_anchors.py",
    "scripts/set_archived_descriptions.py",
    "tests/test_retired_backfill_scripts.py",
    "CHANGELOG.md",
}
# todo/ files are historical artifacts -- they record what a session believed
# when it filed them, and are never edited in place.
ALLOWED_PREFIXES = (
    ".rlsbl/releases/", ".rlsbl/changes/", "docs/_build/", "todo/",
)


@pytest.mark.parametrize("name", sorted(STUBS))
def test_nothing_live_references_the_retired_script(name):
    result = subprocess.run(
        ["git", "grep", "-l", name],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    hits = [line for line in result.stdout.splitlines() if line.strip()]
    live = [
        path for path in hits
        if path not in ALLOWED and not path.startswith(ALLOWED_PREFIXES)
    ]
    assert live == [], (
        f"these files still name the retired {name}; point them at "
        f"`rlsbl release backfill` instead: {live}"
    )
