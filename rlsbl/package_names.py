"""Offline judgement of a package name against a registry's published naming rules.

Each function returns a list of sentences saying why a name cannot be
published under that registry, empty when it can. Nothing here contacts a
registry: availability is ``rlsbl check-name``'s question, not this module's.

- npm: the rules of npm's own ``validate-npm-package-name`` for a NEW package
  (https://github.com/npm/validate-npm-package-name). Its "warnings" -- capital
  letters, more than 214 characters, the characters ``~'!()*``, and a Node core
  module's name -- make a name invalid for a new package, so they are problems
  here too.
- PyPI: the name grammar of PEP 508
  (https://peps.python.org/pep-0508/#names), which PyPI enforces on upload.
"""

import re

#: npm's blocked names.
NPM_BLOCKED_NAMES = frozenset({"node_modules", "favicon.ico"})

#: Node's core module names, which npm refuses for a new package. Generated
#: from Node's own ``require("module").builtinModules`` (the unprefixed,
#: unscoped entries), as validate-npm-package-name does.
NODE_CORE_MODULES = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib",
})

NPM_MAX_LENGTH = 214

#: The characters JavaScript's ``encodeURIComponent`` leaves unescaped. npm
#: requires a name (or each half of a scoped name) to be made of these.
_URL_SAFE = re.compile(r"^[A-Za-z0-9\-_.!~*'()]*$")
_NPM_SPECIAL = re.compile(r"[~'!()*]")
_NPM_SCOPED = re.compile(r"^@([^/]+)/(.+)$")

#: PEP 508's name grammar.
_PEP508_NAME = re.compile(r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$", re.IGNORECASE)


def npm_name_problems(name):
    """Why *name* cannot be a new npm package's name."""
    if name == "":
        return ["an npm package name must not be empty"]
    problems = []
    if name.startswith("."):
        problems.append("an npm package name cannot start with a period")
    if name.startswith("_"):
        problems.append("an npm package name cannot start with an underscore")
    if name.strip() != name:
        problems.append(
            "an npm package name cannot contain leading or trailing spaces"
        )
    if name.lower() in NPM_BLOCKED_NAMES:
        problems.append(f"'{name}' is a blocked npm package name")
    scoped = _NPM_SCOPED.match(name)
    parts = scoped.groups() if scoped else (name,)
    if not all(_URL_SAFE.match(part) for part in parts):
        problems.append(
            "an npm package name can only contain URL-friendly characters "
            "(letters, digits, and - _ . ! ~ * ' ( ))"
        )
    if name.lower() in NODE_CORE_MODULES:
        problems.append(f"'{name}' is a Node core module name")
    if len(name) > NPM_MAX_LENGTH:
        problems.append(
            f"an npm package name cannot be longer than {NPM_MAX_LENGTH} "
            f"characters"
        )
    if name.lower() != name:
        problems.append(
            "an npm package name cannot contain uppercase letters"
        )
    if _NPM_SPECIAL.search(parts[-1]):
        problems.append(
            "an npm package name cannot contain the special characters ~'!()*"
        )
    return problems


def pypi_name_problems(name):
    """Why *name* cannot be a PyPI project's name."""
    if _PEP508_NAME.match(name):
        return []
    return [
        f"'{name}' is not a valid PyPI project name (PEP 508: only letters, "
        f"digits, '.', '_', and '-', beginning and ending with a letter or "
        f"digit)"
    ]
