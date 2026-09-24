"""Every shipped workflow template that installs Go reads the version from a file.

CI must test with the same Go as local development, and that version must be
declared once per repository, never typed into a workflow. A literal
(``go-version: '1.25'``, ``go-version: stable``) drifts from the developer's
toolchain silently: a test that needs the newer toolchain then skips on CI
forever, and nobody sees it.

The declared source is:

* ``go.mod`` for a Go module. ``actions/setup-go`` reads go.mod's ``toolchain``
  line first and falls back to the ``go`` directive, so the module declares the
  development Go in ``toolchain`` while ``go`` stays the consumers' floor
  (``toolchain`` only affects the main module).
* ``.go-version`` for a project that is not a Go module but installs Go to run
  a tool (the pgdesign target installs the pgdesign CLI with ``go install``).

A comment that restates a version in the workflow is a second copy of the fact
and goes stale the same way, so none is rendered.
"""

import glob
import os
import re

import pytest
from ruamel.yaml import YAML

from rlsbl.commands.init_cmd import process_template
from rlsbl.targets.pgdesign import PgdesignTarget

TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates"
)

SETUP_GO_RE = re.compile(r"^(\s*)- uses: .*actions/setup-go")


def _workflow_templates():
    return sorted(glob.glob(os.path.join(TEMPLATES_ROOT, "*", "*.yml.tpl")))


def _setup_go_inputs(text):
    """Map each setup-go step (by line number) to its ``with:`` input keys.

    Line-based on purpose: the templates carry handlebars blocks that are not
    YAML until rendered, and this must see every template, rendered or not.
    """
    lines = text.splitlines()
    found = {}
    for index, line in enumerate(lines):
        match = SETUP_GO_RE.match(line)
        if not match:
            continue
        step_indent = len(match.group(1))
        keys = []
        for follower in lines[index + 1:]:
            stripped = follower.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(follower) - len(follower.lstrip())
            if indent <= step_indent:
                break
            key = stripped.split(":", 1)[0]
            keys.append(key)
        found[index + 1] = keys
    return found


def _templates_installing_go():
    return [
        path for path in _workflow_templates()
        if _setup_go_inputs(open(path, encoding="utf-8").read())
    ]


def _rel(path):
    return os.path.relpath(path, TEMPLATES_ROOT).replace(os.sep, "/")


class TestEveryGoInstallReadsAVersionFile:

    def test_templates_installing_go_are_enumerated(self):
        """Guard for the scan itself: it must find the known Go installers."""
        assert {_rel(p) for p in _templates_installing_go()} >= {
            "go/ci.yml.tpl",
            "go/publish.yml.tpl",
            "go/publish-library.yml.tpl",
            "pgdesign/ci.yml.tpl",
        }

    @pytest.mark.parametrize(
        "path", _templates_installing_go(), ids=_rel,
    )
    def test_setup_go_uses_go_version_file_never_a_literal(self, path):
        text = open(path, encoding="utf-8").read()
        for line_no, keys in _setup_go_inputs(text).items():
            assert "go-version-file" in keys, (
                f"{_rel(path)}:{line_no}: setup-go without go-version-file"
            )
            assert "go-version" not in keys, (
                f"{_rel(path)}:{line_no}: setup-go pins a literal go-version"
            )

    @pytest.mark.parametrize("path", _workflow_templates(), ids=_rel)
    def test_no_template_restates_a_go_version(self, path):
        text = open(path, encoding="utf-8").read()
        assert "minRequiredGo" not in text, _rel(path)
        assert not re.search(r"^\s*go-version:", text, re.MULTILINE), _rel(path)


class TestDeclaredSourcePerTarget:

    @pytest.mark.parametrize(
        "name", ["ci.yml.tpl", "publish.yml.tpl", "publish-library.yml.tpl"],
    )
    def test_go_target_reads_go_mod(self, name):
        text = open(os.path.join(TEMPLATES_ROOT, "go", name), encoding="utf-8").read()
        assert re.search(r"^\s*go-version-file: go\.mod$", text, re.MULTILINE)

    def test_go_ci_names_the_toolchain_line_as_the_source(self):
        """setup-go falls back to the floor without a toolchain line; say so."""
        text = open(os.path.join(TEMPLATES_ROOT, "go", "ci.yml.tpl"), encoding="utf-8").read()
        assert "toolchain" in text

    def test_pgdesign_ci_reads_dot_go_version(self):
        tpl_path = os.path.join(PgdesignTarget().template_dir(), "ci.yml.tpl")
        content, unreplaced = process_template(
            open(tpl_path, encoding="utf-8").read(), {},
        )
        assert unreplaced == []
        doc = YAML(typ="safe").load(content)
        setup_go = [
            step for step in doc["jobs"]["test"]["steps"]
            if step.get("uses", "").startswith("actions/setup-go@")
        ]
        assert [step["with"] for step in setup_go] == [
            {"go-version-file": ".go-version"}
        ]
