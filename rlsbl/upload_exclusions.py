"""The private-path rule in each ecosystem's own exclusion syntax.

The rule itself is :mod:`rlsbl.private_paths`; this module spells it where an
ecosystem needs another syntax: a ``.dockerignore`` entry for a Docker build
context, and the rule's own source for the pypi CI template to embed.
"""


def dockerignore_entry(rule):
    """*rule* (gitignore syntax) in ``.dockerignore`` syntax.

    Docker matches a pattern against the whole context-relative path, so a
    rule that holds at any depth needs a leading ``**/``, and a trailing slash
    means nothing to it.
    """
    if rule.startswith("/"):
        return rule.strip("/")
    return "**/" + rule.rstrip("/")


def ci_check_script(indent):
    """:mod:`rlsbl.private_paths`'s source, indented for a YAML ``run: |`` block.

    The first line is left unindented: the template places the variable at
    the indentation it wants.
    """
    from . import private_paths

    with open(private_paths.__file__, encoding="utf-8") as f:
        lines = f.read().rstrip("\n").split("\n")
    pad = " " * indent
    return "\n".join(
        [lines[0]] + [(pad + line) if line else "" for line in lines[1:]]
    )
