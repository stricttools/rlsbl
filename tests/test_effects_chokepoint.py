"""Backstop: no rlsbl production module bypasses the effect chokepoint.

Every subprocess launch, filesystem mutation, and network call in ``rlsbl/``
must go through ``rlsbl/effects.py``.  This test walks the package with the
``ast`` module and fails on any direct call to the banned stdlib primitives.

It is AST-based rather than grep-based on purpose: ``except
subprocess.CalledProcessError`` and the string ``os.replace`` in a docstring
are not effects, and a grep cannot tell them apart from a call.

Adding a bare ``subprocess.run`` to a production module fails this test.  The
fix is to call the corresponding ``rlsbl.effects`` wrapper -- not to widen
EXEMPT_FILES.
"""

import ast
import os

import pytest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RLSBL_DIR = os.path.join(PROJECT_ROOT, "rlsbl")


# Every exemption needs a reason.  Keep this list tiny.
EXEMPT_FILES = {
    # The chokepoint itself: it IS the authorized stdlib caller.
    "effects.py",
}

EXEMPT_DIRS = {
    # Template payloads rendered into OTHER repos -- data, never imported here.
    "templates",
    # Build/import artifacts, not source.
    "__pycache__",
}


# module attribute -> banned callables reached through it
BANNED_ATTR_CALLS = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output"},
    "os": {
        "replace", "rename", "remove", "unlink", "rmdir", "removedirs",
        "makedirs", "mkdir", "chmod", "symlink", "link", "truncate",
    },
    "shutil": {
        "copy", "copy2", "copyfile", "copytree", "copymode", "copystat",
        "move", "rmtree",
    },
    "request": {"urlopen"},          # urllib.request.urlopen
    "urllib": {"urlopen"},           # urllib.urlopen alias forms
}

# Bare names that are banned when imported directly (``from os import replace``).
BANNED_BARE_NAMES = {
    "urlopen",
}

# Object-method writes (pathlib.Path and friends).  These are matched on the
# attribute name alone -- the receiver's type is not knowable statically -- so
# the set is restricted to names that are unambiguous filesystem mutations.
BANNED_METHOD_NAMES = {
    "write_text",
    "write_bytes",
    "touch",
    "hardlink_to",
    "symlink_to",
}

WRITE_MODE_CHARS = ("w", "a", "x", "+")


def _production_files():
    """Yield every production Python file, honoring the exemption lists."""
    for dirpath, dirnames, filenames in os.walk(RLSBL_DIR):
        dirnames[:] = [d for d in dirnames if d not in EXEMPT_DIRS]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            if os.path.relpath(path, RLSBL_DIR) in EXEMPT_FILES or name in EXEMPT_FILES:
                continue
            yield path


def _module_aliases(tree):
    """Map local names back to the stdlib module they refer to.

    Handles ``import subprocess``, ``import subprocess as _subprocess``,
    ``import urllib.request``, and ``from . import subprocess as _subprocess``
    (rlsbl re-exports stdlib modules from package __init__ files so tests can
    patch them module-scoped).
    """
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                leaf = alias.name.split(".")[-1]
                local = alias.asname or root
                if alias.asname:
                    aliases[local] = leaf
                else:
                    aliases[local] = root
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local = alias.asname or alias.name
                aliases[local] = alias.name
    return aliases


def _violations(path):
    """Return a list of (lineno, description) chokepoint bypasses in *path*."""
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=path)
    aliases = _module_aliases(tree)
    found = []

    for node in ast.walk(tree):
        # ``from subprocess import run`` / ``from shutil import rmtree`` etc.
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[-1]
            banned = BANNED_ATTR_CALLS.get(root, set())
            for alias in node.names:
                if alias.name in banned:
                    found.append(
                        (node.lineno, f"from {node.module} import {alias.name}")
                    )

        if not isinstance(node, ast.Call):
            continue

        func = node.func

        # module.attr(...) -- resolve one or two levels (urllib.request.urlopen)
        if isinstance(func, ast.Attribute):
            base = func.value
            base_name = None
            if isinstance(base, ast.Name):
                base_name = aliases.get(base.id, base.id)
            elif isinstance(base, ast.Attribute):
                base_name = base.attr
            if base_name in BANNED_ATTR_CALLS and func.attr in BANNED_ATTR_CALLS[base_name]:
                found.append((node.lineno, f"{base_name}.{func.attr}(...)"))
                continue
            if func.attr in BANNED_METHOD_NAMES:
                found.append((node.lineno, f".{func.attr}(...)"))
                continue
            if func.attr == "mkdir" and base_name not in BANNED_ATTR_CALLS:
                # Path(...).mkdir(...) -- os.mkdir is caught above.
                found.append((node.lineno, ".mkdir(...)"))
                continue

        # bare name calls: open(..., "w"), urlopen(...), rmtree(...)
        if isinstance(func, ast.Name):
            resolved = aliases.get(func.id, func.id)
            if func.id in BANNED_BARE_NAMES or resolved in BANNED_BARE_NAMES:
                found.append((node.lineno, f"{func.id}(...)"))
                continue
            if func.id == "open":
                mode = None
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    mode = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                if isinstance(mode, str) and any(c in mode for c in WRITE_MODE_CHARS):
                    found.append((node.lineno, f"open(..., {mode!r})"))

    return found


def test_no_production_module_bypasses_the_effect_chokepoint():
    """rlsbl/ calls no effectful stdlib primitive outside rlsbl/effects.py."""
    offenders = []
    for path in sorted(_production_files()):
        rel = os.path.relpath(path, PROJECT_ROOT)
        for lineno, what in _violations(path):
            offenders.append(f"{rel}:{lineno}: {what}")

    assert not offenders, (
        f"{len(offenders)} effect chokepoint bypass(es) -- route these through "
        "rlsbl.effects:\n" + "\n".join(offenders)
    )


def test_chokepoint_module_exists_and_is_exempt():
    """The exemption list names files that actually exist."""
    assert os.path.isfile(os.path.join(RLSBL_DIR, "effects.py"))
    for name in EXEMPT_FILES:
        assert os.path.exists(os.path.join(RLSBL_DIR, name)), name


def test_scanner_detects_a_planted_bypass(tmp_path):
    """The scanner is not vacuously green: it flags a bare subprocess.run."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import subprocess\n"
        "def go():\n"
        "    subprocess.run(['ls'])\n"
        "    with open('x', 'w') as f:\n"
        "        f.write('y')\n",
        encoding="utf-8",
    )
    found = _violations(str(planted))
    descriptions = [what for _, what in found]
    assert "subprocess.run(...)" in descriptions
    assert "open(..., 'w')" in descriptions


def test_scanner_ignores_non_calls(tmp_path):
    """Exception classes and read-mode opens are not effects."""
    benign = tmp_path / "benign.py"
    benign.write_text(
        "import subprocess\n"
        "def go():\n"
        "    try:\n"
        "        pass\n"
        "    except subprocess.CalledProcessError:\n"
        "        pass\n"
        "    with open('x', 'r', encoding='utf-8') as f:\n"
        "        return f.read()\n",
        encoding="utf-8",
    )
    assert _violations(str(benign)) == []


if __name__ == "__main__":  # pragma: no cover - manual triage helper
    pytest.main([__file__, "-q"])
