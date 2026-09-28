"""Nested workspace members: a member whose path lies inside another member's.

The ownership model already gives every file to the most specific declared
member, so nesting needs no new key. These tests pin the fixture shapes every
nested-member test builds on, and the attribution they rely on.
"""

import pytest

from conftest import NESTED_SHAPES, make_nested_workspace
from rlsbl.ownership import owner_name_of


@pytest.mark.parametrize("shape", sorted(NESTED_SHAPES))
def test_every_nested_shape_loads(tmp_path, shape):
    projects = make_nested_workspace(tmp_path / "ws", shape)
    declared = {p["path"] for p in projects if p["path"] != "."}
    assert declared == {path for path, _, _ in NESTED_SHAPES[shape][0]}


@pytest.mark.parametrize(
    "shape, filepath, owner",
    [
        ("go", "draw/draw.go", "draw"),
        ("go", "draw/cmd/main.go", "drawcmd"),
        ("go", "kernel/vulkan/vulkan.go", "vulkan"),
        ("python", "sdk/sgsdk/__init__.py", "sdk"),
        ("python", "sdk/python/tests/test_it.py", "sdkpython"),
        ("python", "sdk/npm/index.js", "sdknpm"),
        ("shared", "gfx/shader/shader.go", "shader"),
        ("shared", "gfx/gfx.go", "gfx"),
    ],
)
def test_the_most_specific_member_owns_a_nested_file(
    tmp_path, shape, filepath, owner,
):
    projects = make_nested_workspace(tmp_path / "ws", shape, commit=False)
    assert owner_name_of(filepath, projects) == owner
