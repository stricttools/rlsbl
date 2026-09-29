"""A member's hook may not run inside a member nested within it.

Every file belongs to one member, and so does every command a hook runs: a
parent's hook whose ``dir`` points into a nested member (``sdk``'s hook with
``dir = "python"``, where ``sdk/python`` is a member) would act on that
member's files under the parent's name. It is refused where the hook runs,
naming the member that owns the directory. A ``dir`` climbing out of the
member with ``..`` is refused too. Hooks keep running once, where they are
declared; there is no per-member form.
"""

import pytest

from conftest import make_nested_workspace
from rlsbl.commands.release.hooks import HookError, run_config_hooks


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    make_nested_workspace(root, "python", commit=False)
    return root


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs.get("cwd"))

    monkeypatch.setattr("rlsbl.commands.release.effects.run", fake_run)
    return calls


def _hooks(entry):
    return {"hooks": {"pre_release": [entry]}}


def test_a_parents_hook_inside_a_nested_member_is_refused(ws, ran):
    with pytest.raises(HookError) as exc:
        run_config_hooks(
            "pre-release", _hooks({"cmd": "pytest", "dir": "python"}),
            str(ws / "sdk"), {}, 60,
        )
    message = str(exc.value)
    assert "dir 'python' lies inside workspace member 'sdkpython' (sdk/python)" in message
    assert "sdk/python/.rlsbl/config.json" in message
    assert ran == []


def test_declaring_the_hook_on_the_nested_member_clears_it(ws, ran):
    # Apply the named fix: the hook moves to the member that owns the directory.
    run_config_hooks(
        "pre-release", _hooks({"cmd": "pytest"}), str(ws / "sdk" / "python"), {}, 60,
    )
    assert ran == [str(ws / "sdk" / "python")]


def test_a_dir_climbing_out_of_the_member_is_refused(ws, ran):
    with pytest.raises(HookError) as exc:
        run_config_hooks(
            "pre-release", _hooks({"cmd": "make", "dir": "../other"}),
            str(ws / "sdk"), {}, 60,
        )
    assert "must name a directory inside" in str(exc.value)


def test_a_dir_inside_the_members_own_territory_runs(ws, ran):
    (ws / "sdk" / "docs").mkdir()
    run_config_hooks(
        "pre-release", _hooks({"cmd": "make", "dir": "docs"}), str(ws / "sdk"), {}, 60,
    )
    assert ran == [str(ws / "sdk" / "docs")]


def test_a_releasable_hook_may_run_in_its_own_member(ws, ran):
    """A releasable-level hook acts for the releasable's members."""
    run_config_hooks(
        "pre-release", _hooks({"cmd": "pytest", "dir": "sdk/python"}),
        str(ws), {}, 60, hook_owners=frozenset({"sdkpython"}),
    )
    assert ran == [str(ws / "sdk" / "python")]
