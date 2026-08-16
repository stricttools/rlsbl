#!/usr/bin/env python3
"""Survey strictcli flag/arg declarations for presence and choices shape.

One-off migration aid for the strictcli 0.41 declaration regime: lists every
`strictcli.flag` / `strictcli.arg` / `strictcli.Flag` / `strictcli.Arg` site
with the declaration facts the new registration checks read (presence,
default, choices shape) plus the enclosing command's effect.

Usage: python scripts/survey_presence.py [path ...]
"""

import ast
import sys
from pathlib import Path

DECL_NAMES = {"flag", "arg", "Flag", "Arg"}


def _kw(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _lit(node):
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except Exception:
        return f"<{ast.dump(node)[:40]}>"


def survey(path):
    tree = ast.parse(path.read_text(), str(path))
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        effect = None
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                fn = dec.func
                if isinstance(fn, ast.Attribute) and fn.attr == "command":
                    effect = _lit(_kw(dec, "effect"))
        decls = []
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = dec.func
            if isinstance(fn, ast.Attribute) and fn.attr in DECL_NAMES:
                decls.append(dec)
            # command(...) can carry args=[Arg(...)] / mutex / flags
            for sub in ast.walk(dec):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in ("Flag", "Arg")
                ):
                    decls.append(sub)
        for d in decls:
            name = _lit(_kw(d, "name"))
            if name is None and d.args:
                name = _lit(d.args[0])
            has_default = _kw(d, "default") is not None
            presence = _lit(_kw(d, "presence"))
            required = _kw(d, "required")
            choices = _kw(d, "choices")
            rows.append(
                {
                    "file": str(path),
                    "line": d.lineno,
                    "kind": d.func.attr,
                    "command": node.name,
                    "effect": effect,
                    "name": name,
                    "presence": presence,
                    "default": _lit(_kw(d, "default")) if has_default else "<none>",
                    "required": _lit(required) if required is not None else "<none>",
                    "choices": _lit(choices) if choices is not None else "<none>",
                }
            )
    return rows


def main(argv):
    paths = [Path(p) for p in argv[1:]] or [Path("rlsbl")]
    files = []
    for p in paths:
        files.extend(sorted(p.rglob("*.py")) if p.is_dir() else [p])
    rows = []
    for f in files:
        rows.extend(survey(f))
    undeclared = [
        r
        for r in rows
        if r["presence"] is None and r["default"] == "<none>" and r["required"] == "<none>"
    ]
    empty_default = [r for r in rows if r["default"] == ""]
    with_choices = [r for r in rows if r["choices"] != "<none>"]
    mutating_default = [
        r for r in rows if r["effect"] == "mutating" and r["default"] not in ("<none>", [], {})
    ]
    print(f"total declarations: {len(rows)}")
    print(f"presence undeclared: {len(undeclared)}")
    for r in undeclared:
        print(f"  {r['file']}:{r['line']} {r['kind']}({r['name']}) cmd={r['command']} effect={r['effect']}")
    print(f'\ndefault="" sentinels: {len(empty_default)}')
    for r in empty_default:
        print(f"  {r['file']}:{r['line']} {r['kind']}({r['name']}) cmd={r['command']} effect={r['effect']}")
    print(f"\nchoices sites: {len(with_choices)}")
    for r in with_choices:
        print(f"  {r['file']}:{r['line']} {r['kind']}({r['name']}) choices={r['choices']}")
    print(f"\nmutating commands with a value default: {len(mutating_default)}")
    for r in mutating_default:
        print(
            f"  {r['file']}:{r['line']} {r['kind']}({r['name']}) cmd={r['command']} default={r['default']!r}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
