"""The npm CI Node matrix is derived from package.json's ``engines.node``.

A scaffolded npm CI used to test on a fixed list of Node lines whatever the
package declared, so a package supporting only Node 22 and later still had
its tests run (and fail) on Node 20. The matrix is now every supported line
whose newest release the declared range admits, and a package declaring no
``engines.node`` is refused with the remedy.
"""

import json

import pytest

from rlsbl.commands.init_cmd import run_cmd
from rlsbl.context import create_context
from rlsbl.errors import ConfigError
from rlsbl.node_matrix import (
    SUPPORTED_NODE_LINES,
    NodeMatrixError,
    node_lines_satisfying,
)


@pytest.mark.parametrize("node_range, lines", [
    (">=20", [20, 22, 24]),
    (">=22", [22, 24]),
    (">= 22.3.0", [22, 24]),
    (">=20.10.0 <24", [20, 22]),
    ("^22 || ^24", [22, 24]),
    ("^22.1.0", [22]),
    ("~24.2", []),
    ("22.x", [22]),
    ("22", [22]),
    ("20 - 22", [20, 22]),
    ("20 - 22.4", [20]),
    ("*", list(SUPPORTED_NODE_LINES)),
    (">20", [22, 24]),
    ("<=22", [20, 22]),
    ("<22", [20]),
    (">=18", [20, 22, 24]),
])
def test_the_lines_a_range_admits(node_range, lines):
    """A line is tested when its newest release -- what setup-node installs
    for ``node-version: <line>`` -- satisfies the range."""
    if lines:
        assert node_lines_satisfying(node_range) == lines
    else:
        with pytest.raises(NodeMatrixError, match="admits none"):
            node_lines_satisfying(node_range)


def test_an_unreadable_range_is_refused():
    with pytest.raises(NodeMatrixError, match="not a range rlsbl can read"):
        node_lines_satisfying(">=twenty")


def _npm_project(root, engines):
    pkg = {"name": "demo-pkg", "version": "0.1.0", "scripts": {"test": "true"}}
    if engines is not None:
        pkg["engines"] = engines
    (root / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
    (root / "package-lock.json").write_text("{}\n")
    rlsbl_dir = root / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    (rlsbl_dir / "config.json").write_text(json.dumps(
        {"targets": ["npm"], "publish_mode": "ci"}, indent=2) + "\n")


def _scaffold(root):
    run_cmd("npm", [], {
        "auto-commit": False, "auto-tag": False, "skip-shared": True,
    }, ctx=create_context(root))


def test_the_scaffolded_matrix_follows_engines_node(mock_git_repo):
    _npm_project(mock_git_repo, {"node": ">=22"})
    _scaffold(mock_git_repo)
    ci = (mock_git_repo / ".github" / "workflows" / "ci.yml").read_text()
    assert "node-version: [22, 24]" in ci
    assert "[20, 22, 24]" not in ci


def test_no_engines_node_is_refused_and_declaring_it_clears_the_refusal(mock_git_repo):
    _npm_project(mock_git_repo, None)
    with pytest.raises(ConfigError) as info:
        _scaffold(mock_git_repo)
    message = str(info.value)
    assert "declares no engines.node" in message
    assert '"engines": {"node": ">=22"}' in message
    assert not (mock_git_repo / ".github" / "workflows" / "ci.yml").exists()

    # The remedy the refusal names, performed.
    _npm_project(mock_git_repo, {"node": ">=22"})
    _scaffold(mock_git_repo)
    ci = (mock_git_repo / ".github" / "workflows" / "ci.yml").read_text()
    assert "node-version: [22, 24]" in ci
