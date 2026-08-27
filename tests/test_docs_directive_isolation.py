"""The docs directives render from the committed matrix, never from an rlsbl import.

The directives used to call rlsbl's introspection functions directly, which put
the package on the documentation environment's dependency list -- and a release
once failed when that environment lost its rlsbl overlay. They now read
``rlsbl/data/support-matrix.json``.

Proving that honestly needs more than reading the source. Each directive is
executed in a subprocess whose import system REFUSES ``rlsbl`` outright: any
attempt to import it raises, and the run also asserts afterwards that no rlsbl
module was ever loaded. A directive that reached for the package would fail the
subprocess rather than quietly succeed because the package happened to be
installed.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DIRECTIVES_DIR = REPO_ROOT / "docs" / "directives"

# Every directive registered in selfdoc.json, with the attrs its resolve takes.
DIRECTIVES = [
    "target_table",
    "feature_matrix",
    "pipeline_table",
    "target_count",
    "check_count",
]


# The subprocess body. It blocks rlsbl at the meta-path level, loads one
# directive file by path, runs resolve(), and reports what it saw.
_PROBE = r'''
import importlib.util
import json
import sys
import types
from pathlib import Path

directive_path = Path(sys.argv[1])


class _RefuseRlsbl:
    """Meta-path finder that makes `import rlsbl` impossible."""

    def find_module(self, fullname, path=None):
        return self.find_spec(fullname, path)

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "rlsbl" or fullname.startswith("rlsbl."):
            raise ImportError(
                f"the docs directives must not import rlsbl (tried {fullname})"
            )
        return None


sys.meta_path.insert(0, _RefuseRlsbl())

# selfdoc-core is the docs renderer, not something the directive derives from.
# Stub it so the probe tests the DATA path rather than selfdoc's installation.
pkg = types.ModuleType("selfdoc_core")
pkg.__path__ = []
tables = types.ModuleType("selfdoc_core.tables")


def _render(headers, rows, **kwargs):
    lines = ["| " + " | ".join(str(h) for h in headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines) + "\n"


tables.render_markdown_table = _render
sys.modules["selfdoc_core"] = pkg
sys.modules["selfdoc_core.tables"] = tables

spec = importlib.util.spec_from_file_location("directive_under_test", directive_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

output = module.resolve({}, None, None)

leaked = sorted(n for n in sys.modules if n == "rlsbl" or n.startswith("rlsbl."))
print(json.dumps({"output": output, "leaked": leaked}))
'''


def _run_probe(directive):
    """Execute one directive in an rlsbl-free subprocess; return its report."""
    import json

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(DIRECTIVES_DIR / f"{directive}.py")],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"{directive}.resolve() failed with rlsbl unimportable:\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestNoDirectiveImportsRlsbl:

    def test_every_registered_directive_is_covered_here(self):
        """selfdoc.json's directive list must not outgrow this test."""
        import json

        with open(REPO_ROOT / "selfdoc.json", encoding="utf-8") as f:
            registered = json.load(f)["directives"]
        modules = sorted(Path(p).stem for p in registered.values())
        assert modules == sorted(DIRECTIVES)

    @pytest.mark.parametrize("directive", DIRECTIVES)
    def test_the_source_names_no_rlsbl_import(self, directive):
        source = (DIRECTIVES_DIR / f"{directive}.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("import rlsbl"), line
            assert not stripped.startswith("from rlsbl"), line

    @pytest.mark.parametrize("directive", DIRECTIVES)
    def test_it_resolves_with_rlsbl_unimportable(self, directive):
        report = _run_probe(directive)
        assert report["output"], f"{directive} rendered nothing"
        assert report["leaked"] == [], (
            f"{directive} imported rlsbl modules: {report['leaked']}"
        )


class TestTheIsolatedOutputIsTheRealOne:
    """The subprocess renders the same content the in-process directive does."""

    def test_target_table_lists_every_target(self):
        import json

        with open(
            REPO_ROOT / "rlsbl" / "data" / "support-matrix.json", encoding="utf-8"
        ) as f:
            matrix = json.load(f)
        output = _run_probe("target_table")["output"]
        for name in matrix["targets"]:
            assert name in output, name

    def test_feature_matrix_names_a_known_check(self):
        output = _run_probe("feature_matrix")["output"]
        assert "library-lint" in output

    def test_pipeline_table_names_a_known_pipeline(self):
        output = _run_probe("pipeline_table")["output"]
        assert "cloudflare-pages" in output

    def test_target_count_is_the_matrix_count_minus_the_three_named(self):
        import json

        with open(
            REPO_ROOT / "rlsbl" / "data" / "support-matrix.json", encoding="utf-8"
        ) as f:
            matrix = json.load(f)
        expected = len(matrix["targets"]) - 3
        assert f"[{expected} more release targets]" in _run_probe("target_count")["output"]

    def test_check_count_reports_the_registered_checks(self):
        import tomllib

        with open(REPO_ROOT / "rlsbl" / "data" / "checks.toml", "rb") as f:
            checks = tomllib.load(f)["checks"]
        assert f"{len(checks)} checks" in _run_probe("check_count")["output"]
