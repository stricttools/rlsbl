"""The require and replace directives of a go.mod, read without the go command.

Both a directive's one-line form (``require M v1.2.3``) and its block form
(``require ( ... )``) are read; ``//`` comments are ignored. Only the pieces
the workspace checks need are kept -- this is not a general go.mod model.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Require:
    path: str
    version: str
    line: int


@dataclass(frozen=True)
class Replace:
    old_path: str
    new_path: str
    new_version: str | None
    line: int

    @property
    def is_local(self) -> bool:
        """Does it point at a directory (Go's rule: ./, ../ or absolute)?"""
        return (
            self.new_path.startswith(("./", "../", "/"))
            or self.new_path in (".", "..")
        )


def _tokens(line):
    return line.split("//", 1)[0].split()


def parse_directives(text):
    """``(requires, replaces)`` declared in go.mod *text*."""
    requires, replaces = [], []
    block = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        tokens = _tokens(raw)
        if not tokens:
            continue
        if block is not None:
            if tokens == [")"]:
                block = None
                continue
            entry = tokens
        elif tokens[0] in ("require", "replace") and tokens[1:] == ["("]:
            block = tokens[0]
            continue
        elif tokens[0] in ("require", "replace"):
            entry = tokens[1:]
            block_kind = tokens[0]
            _add(block_kind, entry, lineno, requires, replaces)
            continue
        else:
            continue
        _add(block, entry, lineno, requires, replaces)
    return requires, replaces


def _add(kind, entry, lineno, requires, replaces):
    if kind == "require" and len(entry) >= 2:
        requires.append(Require(entry[0], entry[1], lineno))
    elif kind == "replace" and "=>" in entry:
        arrow = entry.index("=>")
        left, right = entry[:arrow], entry[arrow + 1:]
        if left and right:
            replaces.append(Replace(
                left[0], right[0], right[1] if len(right) > 1 else None, lineno,
            ))
