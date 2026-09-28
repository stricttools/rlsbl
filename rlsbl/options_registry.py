"""rlsbl's options registry: generated from the check registry, never edited by hand.

Every rlsbl check is an option, ``rlsbl:<check name>``, and so is each check
strictcli registers into rlsbl's app. An error check ranks
``error > warn > off``, a warning check ``warn > off``; the default is the
check's registered severity, except for the options that stand for an adoption
(:data:`ADOPTION_OPTIONS`), which default to ``off``. An option requires the
options of the checks its check ``depends_on``, so none is switched off while a
check depending on it still runs.

The document is ``rlsbl/data/options.toml``, in the shape of strictspec's
built-in ``options-registry`` schema. ``scripts/gen_options_registry.py``
writes it from :func:`render`, and a test holds the shipped file to a fresh
rendering.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
CHECKS_TOML = DATA_DIR / "checks.toml"
REGISTRY_TOML = DATA_DIR / "options.toml"

#: The options-registry schema version the document is written to.
FORMAT_VERSION = 2

#: The scope kind of the options a workspace may switch on for one member: an
#: entry's scope names the member's directory relative to the repository's git
#: root.
PATH_SCOPE = "path"

#: The options that stand for an adoption: each defaults to off, takes a
#: ``path`` scope, and governs a setting in ``.rlsbl/config.json`` that is
#: required while the option is on and refused while it is off
#: (:data:`rlsbl.options.ADOPTION_SETTINGS`).
ADOPTION_OPTIONS = (
    "dep-floors",
    "format",
    "lint",
    "strictspec-certificate-gate",
    "test-sandbox",
    "type-check",
)

#: The checks strictcli registers into rlsbl's app rather than rlsbl's own
#: checks.toml, with the subject and description their options carry. A test
#: holds this table to the checks the framework actually registers.
FRAMEWORK_CHECKS = {
    "cli-test-coverage": (
        "error",
        "tests",
        "Every registered command path appears in the committed test-coverage manifest.",
    ),
    "consequential-grant-agreement": (
        "warn",
        "code",
        "Every command declaring a grant that leaves the process also declares itself consequential.",
    ),
    "effects-bypass": (
        "error",
        "code",
        "No process, filesystem-mutation, or network call reachable from a command handler bypasses the effects handle.",
    ),
    "observe-allowlist-breadth": (
        "warn",
        "code",
        "No proc_observe_allowlist prefix is a single token.",
    ),
}

#: Options that are not checks: name -> (values, default, subject, description).
NON_CHECK_OPTIONS = {
    "test-sandbox": (
        "on > off",
        "off",
        "tests",
        "Whether the repository distributes the sandboxed test runner its test_sandbox settings describe: scaffold renders the runner, and testisolation-floor requires every CI workflow the settings name to invoke it.",
    ),
}

_HEADER = """\
# rlsbl's options registry: every rlsbl check is an option, rlsbl:<check name>,
# validated by strictspec's built-in options-registry schema. Generated from
# rlsbl/data/checks.toml by `scripts/gen_options_registry.py`; never edit it by
# hand.
"""


def _values(severity):
    return "error > warn > off" if severity == "error" else "warn > off"


def declarations():
    """Every option rlsbl declares, as dicts in the registry's field order,
    sorted by name.
    """
    with open(CHECKS_TOML, "rb") as f:
        checks = tomllib.load(f)["checks"]
    options = []
    for name, check in checks.items():
        options.append({
            "name": name,
            "subject": check["subject"],
            "values": _values(check["severity"]),
            "default": "off" if name in ADOPTION_OPTIONS else check["severity"],
            "scope": PATH_SCOPE if name in ADOPTION_OPTIONS else "none",
            "requires": sorted(check["depends_on"]),
            "description": check["description"],
        })
    for name, (severity, subject, description) in FRAMEWORK_CHECKS.items():
        options.append({
            "name": name,
            "subject": subject,
            "values": _values(severity),
            "default": severity,
            "scope": "none",
            "requires": [],
            "description": description,
        })
    for name, (values, default, subject, description) in NON_CHECK_OPTIONS.items():
        options.append({
            "name": name,
            "subject": subject,
            "values": values,
            "default": default,
            "scope": PATH_SCOPE if name in ADOPTION_OPTIONS else "none",
            "requires": [],
            "description": description,
        })
    return sorted(options, key=lambda o: o["name"])


def _quote(value):
    # A TOML basic string and a JSON string agree for the characters a
    # description uses; ensure_ascii=False keeps non-ASCII text readable.
    return json.dumps(value, ensure_ascii=False)


def render():
    """The registry document, as ``rlsbl/data/options.toml`` must hold it."""
    lines = [_HEADER, f"format_version = {FORMAT_VERSION}"]
    for option in declarations():
        lines.append("")
        lines.append("[[option]]")
        for key in ("name", "subject", "values", "default", "scope"):
            lines.append(f"{key} = {_quote(option[key])}")
        requires = ", ".join(_quote(r) for r in option["requires"])
        lines.append(f"requires = [{requires}]")
        lines.append(f"description = {_quote(option['description'])}")
    return "\n".join(lines) + "\n"
