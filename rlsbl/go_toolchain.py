"""Whether each Go module declares the Go it is developed with: go.mod's
``toolchain`` line.

The ``go`` directive is the oldest Go a consumer may build the module with.
The ``toolchain`` line is the Go the project itself develops and tests with,
and ``actions/setup-go`` (``go-version-file: go.mod``) installs it in CI,
falling back to the ``go`` directive only when the line is absent -- so a
module without one has CI testing on its consumers' floor rather than on the
Go its developers run.

Presence is all that is asked. The line is never compared with the Go on the
machine running the check: that is a machine input, and a release-blocking
check reads only what the repository owns.
"""

import os
from dataclasses import dataclass, field

#: The remedy every finding names.
REMEDY = "go mod edit -toolchain=<version>"


@dataclass
class ToolchainVerdict:
    problems: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.problems


def declares_toolchain(go_mod_text):
    """True when *go_mod_text* carries a ``toolchain`` directive."""
    for raw in go_mod_text.splitlines():
        code = raw.split("//", 1)[0].strip()
        parts = code.split()
        if len(parts) == 2 and parts[0] == "toolchain":
            return True
    return False


def evaluate_go_toolchain(repo_root, module_dirs):
    """One problem per module directory whose go.mod has no toolchain line."""
    verdict = ToolchainVerdict()
    for module_dir in module_dirs:
        go_mod = os.path.join(module_dir, "go.mod")
        if not os.path.isfile(go_mod):
            continue
        with open(go_mod, "r", encoding="utf-8") as f:
            text = f.read()
        if declares_toolchain(text):
            continue
        rel = os.path.relpath(go_mod, repo_root)
        where = os.path.dirname(rel) or "."
        verdict.problems.append(
            f"{rel} declares no toolchain line, so CI's setup-go installs the "
            f"`go` directive's version (the oldest Go consumers may use) "
            f"instead of the Go this module is developed with. Declare it: "
            f"`{REMEDY}` in {where} (e.g. -toolchain=go1.26.6), and commit "
            f"go.mod."
        )
    return verdict
