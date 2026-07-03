"""Lint tests for template files under rlsbl/templates/.

Two checks:

1. **Cross-reference consistency**: a bare ``{{#if varName}}`` must not exist
   when a namespaced ``{{target.varName}}`` form is used elsewhere. This
   catches inconsistencies when the same var is used bare in one template
   and namespaced in another.

2. **Target-directory namespacing**: in target-specific template directories
   (everything except ``shared/``), ``{{#if varName}}`` conditionals should
   use namespaced vars (``{{#if target.varName}}``). Bare conditionals in
   target directories indicate a var that should be prefixed with the
   target name, matching the convention established by ``TemplateVars``.
   Vars that are genuinely shared (used across multiple target dirs without
   a namespace prefix) are exempted.
"""

from __future__ import annotations

import os
import re

TEMPLATES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rlsbl", "templates"
)

# Matches {{#if varName}} or {{#if target.varName}}
_IF_RE = re.compile(r"\{\{#if\s+(\w+(?:\.\w+)*)\}\}")

# Matches {{varName}} or {{target.varName}} (excludes {{action "..."}},
# {{#if}}, {{/if}}, and escaped \{{...}} which are handled separately).
_VAR_RE = re.compile(r"(?<!\\)\{\{(\w+(?:\.\w+)*)\}\}")


def _collect_templates():
    """Yield (relative_path, content) for every .tpl file."""
    for dirpath, _dirs, filenames in os.walk(TEMPLATES_ROOT):
        for fname in sorted(filenames):
            if fname.endswith(".tpl"):
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, TEMPLATES_ROOT)
                with open(full, encoding="utf-8") as f:
                    yield rel, f.read()


def _target_dir(rel_path):
    """Extract the target directory from a relative template path.

    Returns the first path component (e.g., "pypi" from "pypi/ci.yml.tpl"),
    or None for paths without a directory.
    """
    parts = rel_path.split(os.sep)
    return parts[0] if len(parts) > 1 else None


def _collect_all_var_references():
    """Collect variable references across all templates.

    Returns:
        bare_if_vars: dict[varName, list[(file, lineno)]] for bare
            ``{{#if varName}}`` occurrences.
        namespaced_suffixes: set of bare suffixes that appear in any
            namespaced form (e.g., ``hasPytest`` if ``pypi.hasPytest``
            exists).
        shared_bare_vars: set of bare var names used in ``{{varName}}``
            (non-conditional) across multiple target directories, indicating
            they are genuinely shared vars, not target-specific.
    """
    bare_if_vars: dict[str, list[tuple[str, int]]] = {}
    namespaced_suffixes: set[str] = set()
    # Track which target dirs use each bare var (non-conditional)
    bare_var_dirs: dict[str, set[str]] = {}

    for rel_path, content in _collect_templates():
        tgt_dir = _target_dir(rel_path)
        for lineno, line in enumerate(content.splitlines(), 1):
            # Collect bare {{#if varName}} (no dot)
            for m in _IF_RE.finditer(line):
                var = m.group(1)
                if "." not in var:
                    bare_if_vars.setdefault(var, []).append((rel_path, lineno))

            # Collect namespaced suffixes from both {{#if t.v}} and {{t.v}}
            for m in _IF_RE.finditer(line):
                var = m.group(1)
                if "." in var:
                    suffix = var.split(".", 1)[1]
                    namespaced_suffixes.add(suffix)
            for m in _VAR_RE.finditer(line):
                var = m.group(1)
                if "." in var:
                    suffix = var.split(".", 1)[1]
                    namespaced_suffixes.add(suffix)
                elif tgt_dir:
                    # Bare var in a target directory
                    bare_var_dirs.setdefault(var, set()).add(tgt_dir)

    # A var is "shared" if it appears bare in 2+ different target dirs
    shared_bare_vars = {
        var for var, dirs in bare_var_dirs.items() if len(dirs) >= 2
    }

    return bare_if_vars, namespaced_suffixes, shared_bare_vars


class TestTemplateConditionalNamespacing:
    """Conditionals in templates must use namespaced vars consistently."""

    def test_no_bare_conditionals_for_namespaced_vars(self):
        """A bare ``{{#if varName}}`` must not exist when a namespaced
        ``{{target.varName}}`` form is used in any template.
        """
        bare_if_vars, namespaced_suffixes, _ = _collect_all_var_references()

        violations = []
        for var, locations in bare_if_vars.items():
            if var in namespaced_suffixes:
                for rel_path, lineno in locations:
                    violations.append(
                        "  {}:{}: bare {{{{#if {}}}}} -- should be"
                        " namespaced (a {{{{target.{}}}}} form exists"
                        " elsewhere)".format(rel_path, lineno, var, var)
                    )

        assert not violations, (
            "Bare {{#if varName}} used for target-specific vars that have"
            " namespaced forms elsewhere:\n" + "\n".join(violations)
        )

    def test_target_dir_conditionals_are_namespaced(self):
        """In target-specific template directories (not shared/), bare
        ``{{#if varName}}`` should be ``{{#if target.varName}}``.

        Exempts vars that are genuinely shared across multiple target
        directories (used bare in non-conditional ``{{varName}}`` form
        in 2+ target dirs).
        """
        bare_if_vars, _, shared_bare_vars = _collect_all_var_references()

        violations = []
        for var, locations in bare_if_vars.items():
            if var in shared_bare_vars:
                continue
            for rel_path, lineno in locations:
                tgt_dir = _target_dir(rel_path)
                if tgt_dir and tgt_dir != "shared":
                    violations.append(
                        "  {}:{}: bare {{{{#if {}}}}} in target dir '{}'"
                        " -- should be {{{{#if {}.{}}}}}".format(
                            rel_path, lineno, var, tgt_dir, tgt_dir, var
                        )
                    )

        assert not violations, (
            "Bare {{#if varName}} in target-specific template directories"
            " should use namespaced form:\n" + "\n".join(violations)
        )
