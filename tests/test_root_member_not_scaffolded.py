"""`rlsbl scaffold` does not scaffold the workspace root, so nothing tells the
root member to run it.

The root member (``path = "."``) is not scaffolded: scaffold skips the
workspace root, and the root member's CI comes from ``rlsbl monorepo sync``.
A message that names ``rlsbl scaffold`` as the fix for something missing at
the root names a fix that does nothing. Each test here applies the fix the
message names instead, and the failure clears.
"""

import os
import subprocess
import sys

import pytest

from rlsbl.targets import TARGETS


def _workspace_root(tmp_path):
    root = tmp_path / "ws"
    (root / ".rlsbl-monorepo").mkdir(parents=True)
    (root / ".rlsbl-monorepo" / "workspace.toml").write_text(
        '[[releasables]]\nname = "top"\ntag_format = "v{version}"\n\n'
        '[[projects]]\npath = "."\nname = "root"\nreleasable = "top"\n'
    )
    return root


@pytest.mark.parametrize("target", ["go", "plain", "docker", "swift", "zig"])
def test_a_missing_version_file_at_the_root_names_a_hand_edit(tmp_path, target):
    root = _workspace_root(tmp_path)

    with pytest.raises(FileNotFoundError) as exc:
        TARGETS[target].read_version(str(root))
    message = str(exc.value)
    assert "rlsbl scaffold' first" not in message
    assert "does not scaffold the workspace root" in message
    assert "create VERSION there by hand" in message

    (root / "VERSION").write_text("0.3.0\n")
    assert TARGETS[target].read_version(str(root)) == "0.3.0"


def test_a_missing_version_file_in_a_member_still_names_scaffold(tmp_path):
    root = _workspace_root(tmp_path)
    member = root / "kernel"
    member.mkdir()

    with pytest.raises(FileNotFoundError) as exc:
        TARGETS["go"].read_version(str(member))
    assert "Run 'rlsbl scaffold' first." in str(exc.value)


def test_scaffold_at_the_root_says_it_does_not_scaffold_it(tmp_path):
    root = _workspace_root(tmp_path)
    (root / "go.mod").write_text("module example.com/top\n\ngo 1.22\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)

    result = subprocess.run(
        [sys.executable, "-P", "-m", "rlsbl", "scaffold", "--no-auto-commit"],
        cwd=root, capture_output=True, text=True,
    )
    output = result.stdout + result.stderr
    assert "Run 'rlsbl scaffold' again" not in output
    assert "does not scaffold the workspace root" in output
    assert not os.path.exists(root / ".rlsbl")
