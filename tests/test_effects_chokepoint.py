"""Backstop: no rlsbl production module bypasses the effect chokepoint.

Every subprocess launch, filesystem mutation, and network call in ``rlsbl/``
must go through ``rlsbl/effects.py``.  This test walks the package with the
``ast`` module and fails on any direct call to the banned stdlib primitives.

It is AST-based rather than grep-based on purpose: ``except
subprocess.CalledProcessError`` and the string ``os.replace`` in a docstring
are not effects, and a grep cannot tell them apart from a call.

The banned set covers five classes, each of which had real bypasses:
subprocess, filesystem (including the descriptor-level ``os.open`` /
``os.fdopen`` / ``os.write`` shape, which is a complete write that mentions
no banned name), ``tempfile`` (whose entries are created in EVERY mode, so
reaching for it directly makes a preview impure), the ``gh`` network seam,
and raw network (``socket``, ``http.client``, ``requests``).

Adding a bare ``subprocess.run`` to a production module fails this test.  The
fix is to call the corresponding ``rlsbl.effects`` wrapper -- not to widen
EXEMPT_FILES.  The exemption list is exactly two files, both of them parts of
the chokepoint itself; there is deliberately no per-site exemption mechanism,
because a legitimate exception (the advisory lock's real fd, the TCP health
probe) is expressible as a named seam in ``effects.py`` and reads better
there than as an entry in an allowlist nobody revisits.
"""

import ast
import os

import pytest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RLSBL_DIR = os.path.join(PROJECT_ROOT, "rlsbl")


# Every exemption needs a reason.  Keep this list tiny.
EXEMPT_FILES = {
    # The chokepoint's public surface: it routes each operation either to the
    # strictcli effects handle or to the primitives below.
    "effects.py",
    # The primitives themselves: the one authorized stdlib caller.  Split out
    # of effects.py so that strictcli's own effects-bypass lint -- which roots
    # at handlers and at functions reaching for ``ctx.effects`` -- does not
    # walk into them.
    "_effects_direct.py",
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
        # The descriptor-level shapes: os.open + os.fdopen + os.write is a
        # complete write that never touches ``open`` or a banned name above,
        # which is how the exclusive-create release-file writes sat outside
        # the chokepoint unnoticed.
        "open", "fdopen", "write", "close", "renames", "chown", "mkfifo",
    },
    "shutil": {
        "copy", "copy2", "copyfile", "copytree", "copymode", "copystat",
        "move", "rmtree",
    },
    # Temp files and dirs are filesystem writes like any other, and the ones
    # that matter most: they are created in EVERY mode, so a call site that
    # reaches for tempfile directly makes its own preview impure.  Route them
    # through effects.mkdtemp / effects.temp_file (or, when the consumer is an
    # allowlisted observe, effects.observe_scratch_files).
    "tempfile": {
        "mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryDirectory",
        "TemporaryFile", "SpooledTemporaryFile",
        # ``mktemp`` creates nothing itself, so the write it invites is the
        # raw ``open(path, "w")`` on the next line -- which the scanner does
        # catch.  It is banned by name anyway: the name is the request for
        # that write, and naming it here points the fix at effects.temp_file
        # instead of at whichever spelling of the open slipped past.
        "mktemp",
    },
    "request": {"urlopen"},          # urllib.request.urlopen
    "urllib": {"urlopen"},           # urllib.urlopen alias forms
    # Network below the urllib seam: a raw socket or a second HTTP client
    # would reach a registry or a deploy host without appearing in the
    # enumerable network surface.
    "socket": {"create_connection", "create_server", "socket", "socketpair"},
    "client": {"HTTPConnection", "HTTPSConnection"},   # http.client.*
    "requests": {
        "get", "post", "put", "delete", "patch", "head", "options",
        "request", "Session",
    },
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

# The one receiver on which those names are not a bypass but the chokepoint
# itself: ``effects.write_text(...)`` IS the sanctioned seam, and the scanner
# cannot tell it from ``Path(...).write_text(...)`` by attribute name alone.
# Every call on this receiver goes through rlsbl/effects.py by construction, so
# the exemption is the receiver and nothing else -- a banned name on any other
# object is still a finding.
CHOKEPOINT_RECEIVERS = {"effects", "_direct"}

WRITE_MODE_CHARS = ("w", "a", "x", "+")

# The authorized spellings of a gh CLI invocation.  Everything else that puts
# "gh" in argv position 0 is reaching GitHub outside the named network seam.
GH_ENTRY_POINTS = {
    "gh", "gh_argv", "run_gh", "run_gh_unscoped",
    # Not an invocation at all: ``ObserveEntry(("gh", ...), ...)`` DECLARES an
    # allowlist prefix in rlsbl/observe_allowlist.py. Nothing is executed.
    "ObserveEntry",
}


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
                if base_name not in CHOKEPOINT_RECEIVERS:
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


def _gh_bypasses(path):
    """Return (lineno, description) for gh invocations outside effects.gh.

    ``effects.run(["gh", "release", "create", ...])`` passes the subprocess
    chokepoint but escapes the NAMED network seam, so the mutating GitHub
    verbs would stop being enumerable.  This catches "gh" in argv position 0
    of any call that is not one of the authorized entry points.
    """
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    found = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if callee in GH_ENTRY_POINTS:
            continue

        first = node.args[0]
        # run("gh", [...]) -- the utils.run shape
        if isinstance(first, ast.Constant) and first.value == "gh":
            found.append((node.lineno, f"{callee}('gh', ...)"))
            continue
        # effects.run(["gh", ...]) -- the argv-list shape
        if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
            head = first.elts[0]
            if isinstance(head, ast.Constant) and head.value == "gh":
                found.append((node.lineno, f"{callee}(['gh', ...])"))

    return found


def test_no_production_module_invokes_gh_outside_the_network_seam():
    """Every gh call goes through effects.gh (directly or via run_gh*)."""
    offenders = []
    for path in sorted(_production_files()):
        rel = os.path.relpath(path, PROJECT_ROOT)
        for lineno, what in _gh_bypasses(path):
            offenders.append(f"{rel}:{lineno}: {what}")

    assert not offenders, (
        f"{len(offenders)} gh invocation(s) outside the network seam -- call "
        "effects.gh, utils.run_gh (repo-scoped) or utils.run_gh_unscoped:\n"
        + "\n".join(offenders)
    )


def test_gh_scanner_detects_a_planted_bypass(tmp_path):
    """The gh scanner is not vacuously green."""
    planted = tmp_path / "planted_gh.py"
    planted.write_text(
        "from rlsbl import effects\n"
        "def go():\n"
        "    effects.run(['gh', 'release', 'create', 'v1'])\n"
        "    run('gh', ['auth', 'status'])\n"
        "    effects.gh(['release', 'list'])\n"
        "    run_gh(['release', 'list'])\n",
        encoding="utf-8",
    )
    descriptions = [what for _, what in _gh_bypasses(str(planted))]
    assert descriptions == ["run(['gh', ...])", "run('gh', ...)"]


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


def test_scanner_separates_the_chokepoints_writers_from_a_pathlib_write(tmp_path):
    """``effects.write_text`` is the seam; ``Path(...).write_text`` is a bypass.

    The two are one attribute name apart and the scanner cannot type the
    receiver, so the exemption is asserted in both directions: the sanctioned
    spelling produces no finding, and every other receiver still does.
    """
    planted = tmp_path / "planted_seam.py"
    planted.write_text(
        "from rlsbl import effects\n"
        "from pathlib import Path\n"
        "def go(p):\n"
        "    effects.write_text(p, 'through the seam')\n"
        "    effects.write_bytes(p, b'also')\n"
        "    Path(p).write_text('around it')\n"
        "    p.touch()\n",
        encoding="utf-8",
    )
    descriptions = [what for _, what in _violations(str(planted))]
    assert descriptions.count(".write_text(...)") == 1
    assert ".write_bytes(...)" not in descriptions
    assert ".touch(...)" in descriptions


def test_scanner_detects_a_planted_tempfile_bypass(tmp_path):
    """Temp files and dirs are writes, and impure ones: they happen in every mode."""
    planted = tmp_path / "planted_temp.py"
    planted.write_text(
        "import tempfile\n"
        "from tempfile import mkdtemp\n"
        "def go():\n"
        "    a = tempfile.mkdtemp(prefix='x-')\n"
        "    b = tempfile.mkstemp()\n"
        "    c = tempfile.NamedTemporaryFile(delete=False)\n"
        "    d = tempfile.TemporaryDirectory()\n"
        "    e = mkdtemp()\n"
        "    f = tempfile.mktemp(suffix='.json')\n"
        "    with open(f, 'w') as fh:\n"
        "        fh.write('{}')\n"
        "    return a, b, c, d, e, f\n",
        encoding="utf-8",
    )
    # Sorted: ast.walk is breadth-first, so the call inside the ``with`` header
    # is not reported in source order.
    descriptions = sorted(what for _, what in _violations(str(planted)))
    # The bare ``mkdtemp()`` call is not matched by name (too many innocent
    # locals would be), but it cannot exist without the import above it,
    # which is -- exactly as for ``from shutil import rmtree``.
    #
    # ``mktemp`` names a path instead of creating one, so the write it invites
    # is the ``open(f, 'w')`` below it -- flagged on its own.  Both are
    # reported, and the mktemp line is the one that points at effects.temp_file.
    assert descriptions == sorted([
        "from tempfile import mkdtemp",
        "tempfile.mkdtemp(...)",
        "tempfile.mkstemp(...)",
        "tempfile.NamedTemporaryFile(...)",
        "tempfile.TemporaryDirectory(...)",
        "tempfile.mktemp(...)",
        "open(..., 'w')",
    ]), descriptions


def test_scanner_detects_a_planted_descriptor_bypass(tmp_path):
    """os.open + os.fdopen + os.write is a whole write with no banned ``open``."""
    planted = tmp_path / "planted_fd.py"
    planted.write_text(
        "import os\n"
        "def go(path):\n"
        "    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)\n"
        "    os.write(fd, b'x')\n"
        "    os.close(fd)\n"
        "    with os.fdopen(fd, 'w') as f:\n"
        "        f.write('y')\n"
        "    os.renames(path, path + '.bak')\n"
        "    os.chown(path, 0, 0)\n"
        "    os.mkfifo(path)\n",
        encoding="utf-8",
    )
    # Sorted: ast.walk is breadth-first, so a call nested in a ``with`` body
    # is not reported in source order.
    descriptions = sorted(what for _, what in _violations(str(planted)))
    assert descriptions == sorted([
        "os.open(...)",
        "os.write(...)",
        "os.close(...)",
        "os.fdopen(...)",
        "os.renames(...)",
        "os.chown(...)",
        "os.mkfifo(...)",
    ]), descriptions


def test_scanner_detects_a_planted_network_bypass(tmp_path):
    """A raw socket or a second HTTP client escapes the named network seam."""
    planted = tmp_path / "planted_net.py"
    planted.write_text(
        "import http.client\n"
        "import requests\n"
        "import socket\n"
        "def go():\n"
        "    socket.create_connection(('h', 1))\n"
        "    socket.socket()\n"
        "    http.client.HTTPSConnection('h')\n"
        "    requests.get('https://example.invalid')\n"
        "    requests.post('https://example.invalid')\n"
        "    socket.gethostname()\n",
        encoding="utf-8",
    )
    descriptions = [what for _, what in _violations(str(planted))]
    assert descriptions == [
        "socket.create_connection(...)",
        "socket.socket(...)",
        "client.HTTPSConnection(...)",
        "requests.get(...)",
        "requests.post(...)",
    ], descriptions


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
