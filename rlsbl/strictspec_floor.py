"""Every committed validator must declare a format the runtime reads.

A strictspec-generated validator declares the SHAPE it was written to as an
integer ``GENERATED_CODE_FORMAT`` and calls
``strictspec.require_generated_code_format(GENERATED_CODE_FORMAT, GENERATED_BY)``
AT IMPORT. Every strictspec runtime declares the inclusive range of formats it
reads, and pairing succeeds whenever the declared format is in that range --
whatever release produced the file.

So an ordinary strictspec release does not stale a committed validator, and the
one thing that does is a change to the emitted shape, which moves the format.
A validator declaring a format outside the range, or declaring none at all
because it predates the declaration, raises on import -- before any document is
validated, so `rlsbl status` and everything else stops working for whoever
installed the artifact. The remedy is always the same: regenerate.

What is compared
----------------

``strictspec.toml`` is the manifest that declares which validators exist and
where they are written, so it is the enumeration this check reads: no directory
is guessed at. For every declared python output that exists, the
``GENERATED_CODE_FORMAT`` constant is read and held against the range the
linked strictspec runtime declares.

What is NOT compared
--------------------

No dependency floor. ``GENERATED_BY`` stays in each generated file as the
release that produced it, and it is informational: pairing is on the format, so
deriving a ``strictspec>=`` floor from the stamp would demand a regeneration
and a re-release of every consumer on every strictspec release -- the outage
the format declaration exists to end. The ordinary floor is ``dep-floors``'s
business.
"""

import os
import re
import tomllib

#: ``GENERATED_CODE_FORMAT = 1`` as the python generator writes it.
_GENERATED_CODE_FORMAT = re.compile(
    r"""^GENERATED_CODE_FORMAT\s*=\s*(\d+)""", re.M
)

#: The dependency whose generated code this check polices.
PACKAGE = "strictspec"

MANIFEST = "strictspec.toml"

#: The one remedy every finding names.
REMEDY = "regenerate with `strictspec gen`"


class StrictspecGeneratedFormatVerdict:
    """Result of holding each generated validator to the runtime's range."""

    def __init__(self, *, problems=None, notes=None, skip_reason=None):
        self.problems = list(problems or [])
        self.notes = list(notes or [])
        self.skip_reason = skip_reason

    @property
    def ok(self):
        return not self.problems


def accepted_format_range():
    """``(min, max)`` generated-code format the linked runtime reads."""
    import strictspec

    return (
        int(strictspec.MIN_GENERATED_CODE_FORMAT),
        int(strictspec.MAX_GENERATED_CODE_FORMAT),
    )


def reads_format(generated_code_format):
    """Whether the linked runtime reads this generated-code format.

    The verdict is the runtime's own predicate rather than a second
    implementation of it, so the two can never disagree.
    """
    import strictspec

    return strictspec.check_generated_code_format(
        generated_code_format, "unread"
    ) is None


def read_generated_code_format(path):
    """The ``GENERATED_CODE_FORMAT`` constant of a generated file, or None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    m = _GENERATED_CODE_FORMAT.search(text)
    return int(m.group(1)) if m else None


def declared_targets(project_root):
    """``[(output_path, lang)]`` every schema target the manifest declares.

    Returns None when there is no readable ``strictspec.toml`` -- a project
    that generates no validators, which is not this check's business.
    """
    path = os.path.join(str(project_root), MANIFEST)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    targets = []
    for schema in data.get("schemas") or []:
        if not isinstance(schema, dict):
            continue
        for target in schema.get("targets") or []:
            if not isinstance(target, dict):
                continue
            output = target.get("output")
            if isinstance(output, str) and output:
                targets.append((output, target.get("lang")))
    return targets


def evaluate_strictspec_generated_format(project_root):
    """Hold every generated validator to the runtime's accepted format range."""
    root = str(project_root)
    targets = declared_targets(root)
    if targets is None:
        return StrictspecGeneratedFormatVerdict(
            skip_reason=f"no {MANIFEST} -- this project generates no validators",
        )
    if not targets:
        return StrictspecGeneratedFormatVerdict(
            skip_reason=f"{MANIFEST} declares no generation target",
        )

    low, high = accepted_format_range()
    problems = []
    notes = []
    read = {}
    for output, lang in targets:
        path = os.path.join(root, output)
        if not os.path.isfile(path):
            notes.append(f"{output}: declared but not generated yet")
            continue
        declared = read_generated_code_format(path)
        if declared is None:
            if lang == "python":
                problems.append(
                    f"{output}: no GENERATED_CODE_FORMAT, so it predates the "
                    f"format declaration and every {PACKAGE} runtime refuses "
                    f"it at import rather than reading it as format {low}. "
                    f"The remedy is to {REMEDY}."
                )
            else:
                notes.append(
                    f"{output}: no GENERATED_CODE_FORMAT to read (lang {lang})"
                )
            continue
        if not reads_format(declared):
            problems.append(
                f"{output}: declares generated-code format {declared}, which "
                f"the linked {PACKAGE} runtime does not read (it reads "
                f"{low}..{high}), so its import raises before any document is "
                f"validated. The remedy is to {REMEDY}."
            )
            continue
        read[output] = declared

    if read:
        notes.insert(
            0,
            f"{len(read)} generated validator(s) declare a format the "
            f"{PACKAGE} runtime reads ({low}..{high})",
        )
    elif not problems and not notes:
        notes.append("no generated validator declares a format")
    return StrictspecGeneratedFormatVerdict(problems=problems, notes=notes)
