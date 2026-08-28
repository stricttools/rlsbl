"""Every path `.gitattributes` marks as generated is a file that exists.

`.gitattributes` names rlsbl's strictspec-generated validators so git treats
them as generated (linguist, eol normalization). The list is hand-maintained
while the files themselves are produced from `strictspec.toml`, so the two can
drift: a validator that was renamed or never generated leaves a line naming a
path nothing produces, and a reader takes that line as evidence the file is
part of the tree.

Both directions are pinned here: every named path exists, and every generated
Python validator `strictspec.toml` declares is named.
"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GITATTRIBUTES = REPO_ROOT / ".gitattributes"


def _generated_paths():
    """The paths `.gitattributes` marks `linguist-generated=true`."""
    paths = []
    for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "linguist-generated=true" not in stripped:
            continue
        paths.append(stripped.split()[0])
    return paths


def _declared_python_outputs():
    """The Python validator outputs `strictspec.toml` declares."""
    with open(REPO_ROOT / "strictspec.toml", "rb") as f:
        spec = tomllib.load(f)
    outputs = []
    for schema in spec.get("schemas", []):
        for target in schema.get("targets", []):
            if target.get("lang") == "python" and target.get("output"):
                outputs.append(target["output"])
    return outputs


class TestGitattributesNamesRealFiles:

    def test_every_generated_path_exists(self):
        missing = [p for p in _generated_paths() if not (REPO_ROOT / p).is_file()]
        assert missing == [], (
            f".gitattributes marks these paths as generated, but nothing "
            f"produces them: {missing}. A line naming a file that does not "
            f"exist reads as evidence the file is part of the tree."
        )

    def test_every_declared_validator_is_marked(self):
        named = set(_generated_paths())
        unmarked = [p for p in _declared_python_outputs() if p not in named]
        assert unmarked == [], (
            f"strictspec.toml generates these validators, but .gitattributes "
            f"does not mark them generated: {unmarked}"
        )
