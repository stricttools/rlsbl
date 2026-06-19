"""Test for namespace package false positive bug.

When a workspace project is imported via a namespace package
(e.g., ``from orxt.protocols import Tool`` where ``protocols`` is
a workspace member distributed under the ``orxt`` namespace),
``PythonImportScanner`` extracts only the top-level module name
(``orxt``) and fails to match it against the workspace name
(``protocols``).  The dependency appears unused even though it is
imported.
"""

import pytest

from rlsbl.import_scanners import PythonImportScanner


@pytest.mark.xfail(reason="namespace package imports not yet supported")
class TestNamespacePackageImports:
    """PythonImportScanner should detect workspace packages imported via namespace prefixes."""

    def test_from_namespace_dot_package_import(self, tmp_path):
        """``from orxt.protocols import Tool`` should detect ``protocols`` as used.

        Current behavior: the scanner extracts ``orxt`` (the top-level
        module), normalizes it, and checks whether ``orxt`` is in the
        workspace names set ``{"protocols"}``.  It is not, so the import
        is invisible and the dependency appears unused.

        Expected behavior: the scanner should also check sub-components
        of dotted module names against workspace names, detecting that
        ``protocols`` is a workspace package accessed via the ``orxt``
        namespace.
        """
        (tmp_path / "app.py").write_text(
            "from orxt.protocols import Tool\n"
        )
        scanner = PythonImportScanner()
        results = scanner.scan(str(tmp_path), workspace_names={"protocols"})
        names = {r.package_name for r in results}
        assert "protocols" in names

    def test_import_namespace_dot_package(self, tmp_path):
        """``import orxt.protocols`` should detect ``protocols`` as used.

        Same root cause: ``import orxt.protocols`` extracts ``orxt``
        as the top-level module, missing the ``protocols`` component.
        """
        (tmp_path / "app.py").write_text(
            "import orxt.protocols\n"
        )
        scanner = PythonImportScanner()
        results = scanner.scan(str(tmp_path), workspace_names={"protocols"})
        names = {r.package_name for r in results}
        assert "protocols" in names

    def test_deep_namespace_import(self, tmp_path):
        """``from orxt.protocols.grpc import Channel`` should detect ``protocols``.

        Even with a deeper dotted path, the workspace package
        ``protocols`` sitting at the second component must be found.
        """
        (tmp_path / "app.py").write_text(
            "from orxt.protocols.grpc import Channel\n"
        )
        scanner = PythonImportScanner()
        results = scanner.scan(str(tmp_path), workspace_names={"protocols"})
        names = {r.package_name for r in results}
        assert "protocols" in names

    def test_multiple_namespace_packages(self, tmp_path):
        """Multiple workspace packages under the same namespace are all detected.

        Given workspace_names={"protocols", "transport"} and imports of
        ``from orxt.protocols import Tool`` and ``from orxt.transport import Bus``,
        both should be detected.
        """
        (tmp_path / "app.py").write_text(
            "from orxt.protocols import Tool\n"
            "from orxt.transport import Bus\n"
        )
        scanner = PythonImportScanner()
        results = scanner.scan(
            str(tmp_path), workspace_names={"protocols", "transport"},
        )
        names = {r.package_name for r in results}
        assert names == {"protocols", "transport"}
