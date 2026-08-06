"""The scaffolded pgdesign CI workflow installs a real version and runs a real command.

Two independent defects made every scaffolded pgdesign CI red on the first run:

* ``go install ...@latest`` resolved a phantom ``v1.0.0`` that is permanently
  cached on the Go module proxy. It cannot be retracted -- retracting it would
  itself require cutting a ``1.x`` tag, which the project forbids -- so the only
  fix is to install with ``@v0`` and say why in a comment that outlives the
  person who wrote it.
* ``pgdesign validate .`` was deleted in pgdesign 0.12.0. The replacement is
  ``pgdesign check --tag validation``.

Assertions are render-level: the template is rendered exactly as scaffold would
render it, then parsed as a workflow, and the two run steps are read off the
parsed job. Comment-only lines are stripped before the negative matches, because
the ``@v0`` comment names ``@latest`` on purpose.
"""

import os

from ruamel.yaml import YAML

from rlsbl.commands.init_cmd import process_template
from rlsbl.targets.pgdesign import PgdesignTarget

PHANTOM_COMMENT = (
    "# @v0: a phantom v1.0.0 is permanently cached on the Go module proxy; "
    "@latest resolves it"
)


def read_template():
    tpl_path = os.path.join(PgdesignTarget().template_dir(), "ci.yml.tpl")
    with open(tpl_path, "r", encoding="utf-8") as f:
        return f.read()


def render_template():
    content, unreplaced = process_template(read_template(), {})
    assert unreplaced == [], f"unreplaced template vars: {unreplaced}"
    return content


def executable_lines(text):
    """Template text with comment-only lines removed.

    The ``@v0`` comment mentions ``@latest`` deliberately, so negative matches
    must look only at lines that actually run.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def run_steps(content):
    doc = YAML(typ="safe").load(content)
    assert doc is not None, "rendered CI is not a valid workflow document"
    steps = doc["jobs"]["test"]["steps"]
    return [step["run"] for step in steps if "run" in step]


class TestGoInstallPinnedToV0:
    """The install step must pin the major version, never float to @latest."""

    def test_install_step_pins_v0(self):
        install = [r for r in run_steps(render_template()) if r.startswith("go install")]
        assert install == [
            "go install github.com/smm-h/pgdesign/cmd/pgdesign@v0"
        ]

    def test_no_latest_in_any_run_step(self):
        for run in run_steps(render_template()):
            assert "@latest" not in run

    def test_no_latest_outside_comments(self):
        assert "@latest" not in executable_lines(render_template())

    def test_phantom_version_is_explained_in_place(self):
        """A bare @v0 reads like a typo; the reason must ship with it."""
        assert PHANTOM_COMMENT in render_template()


class TestSchemaValidationUsesCheck:
    """``pgdesign validate`` was deleted in 0.12.0; ``check --tag`` replaced it."""

    def test_check_step_present(self):
        runs = run_steps(render_template())
        assert "pgdesign check --tag validation" in runs

    def test_deleted_validate_command_is_gone(self):
        assert "pgdesign validate" not in executable_lines(render_template())

    def test_exactly_one_schema_step_after_the_install(self):
        runs = run_steps(render_template())
        pgdesign_runs = [r for r in runs if r.startswith("pgdesign ")]
        assert pgdesign_runs == ["pgdesign check --tag validation"]
        assert runs.index("pgdesign check --tag validation") > runs.index(
            "go install github.com/smm-h/pgdesign/cmd/pgdesign@v0"
        )


class TestTemplateStillRenders:
    """Editing run lines must not disturb the surrounding workflow document."""

    def test_workflow_shape_survives(self):
        doc = YAML(typ="safe").load(render_template())
        assert doc["name"] == "CI"
        assert doc["jobs"]["test"]["runs-on"] == "ubuntu-latest"
        uses = [s["uses"] for s in doc["jobs"]["test"]["steps"] if "uses" in s]
        assert any(u.startswith("actions/checkout@") for u in uses)
        assert any(u.startswith("actions/setup-go@") for u in uses)
