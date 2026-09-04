"""The release's schema-version patch must preserve every other byte.

`rlsbl release run` re-dumps a strictcli consumer's `.strictcli/schema.json`
and then writes the new version into it. The patch used to be
`json.dumps(json.load(f), indent=2)`, which re-encodes the WHOLE document with
Python's own defaults -- and strictcli writes that file in its own canonical
encoding (schema v2, contract 25.8): raw UTF-8 with `ensure_ascii=False`, no
HTML escaping, canonical floats, two-space indent, one trailing newline.

The two encodings disagree on real content. Every non-ASCII character in any
help text -- and rlsbl's own help is full of em dashes -- came back out as a
`\\uXXXX` escape, so every consumer release rewrote its schema file into
something no strictcli implementation would ever have written, and the next
dump reverted it. The patch is textual now: one key's line, nothing else.
"""

import json

import pytest

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    _patch_schema_version,
)


# A schema fragment in strictcli's canonical v2 encoding, carrying every shape
# `json.dumps(..., indent=2)` renders differently: a non-ASCII em dash, an
# HTML-significant character, a canonical float, an empty container, and a
# NESTED "version" key that the patch must not touch.
_CANONICAL_SCHEMA = """\
{
  "schema_version": 2,
  "name": "demo",
  "version": "0.1.0",
  "help": "a demo — with an em dash & an ampersand",
  "threshold": 1e-7,
  "defaults": {},
  "commands": [
    {
      "name": "show",
      "flags": [
        {
          "name": "version",
          "presence": "optional",
          "value_schema": {
            "type": "string"
          }
        }
      ]
    }
  ]
}
"""


def _write_schema(tmp_path, text=_CANONICAL_SCHEMA):
    schema_dir = tmp_path / ".strictcli"
    schema_dir.mkdir()
    path = schema_dir / "schema.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_patch_rewrites_only_the_version_line(tmp_path):
    """Every byte but the version value survives the patch."""
    path = _write_schema(tmp_path)

    _patch_schema_version(str(tmp_path), "9.9.9")

    after = path.read_text(encoding="utf-8")
    expected = _CANONICAL_SCHEMA.replace('"version": "0.1.0"', '"version": "9.9.9"', 1)
    assert after == expected


def test_patch_preserves_non_ascii_and_unescaped_html(tmp_path):
    """The em dash stays an em dash and `&` stays `&`."""
    path = _write_schema(tmp_path)

    _patch_schema_version(str(tmp_path), "2.0.0")

    after = path.read_text(encoding="utf-8")
    assert "an em dash & an ampersand" in after
    assert "\\u2014" not in after
    assert "\\u0026" not in after


def test_patch_preserves_the_canonical_float_form(tmp_path):
    """`1e-7` is the canonical float form; Python's repr writes `1e-07`."""
    path = _write_schema(tmp_path)

    _patch_schema_version(str(tmp_path), "2.0.0")

    after = path.read_text(encoding="utf-8")
    assert '"threshold": 1e-7' in after
    assert "1e-07" not in after


def test_patch_leaves_a_nested_version_key_alone(tmp_path):
    """Only the top-level `version` is the document's version."""
    path = _write_schema(tmp_path)

    _patch_schema_version(str(tmp_path), "3.1.4")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "3.1.4"
    assert data["commands"][0]["flags"][0]["name"] == "version"
    assert '"name": "version"' in path.read_text(encoding="utf-8")


def test_patch_escapes_the_new_version_as_a_json_string(tmp_path):
    """The replacement value is written as a JSON string literal, not spliced raw."""
    path = _write_schema(tmp_path)

    _patch_schema_version(str(tmp_path), '1.0.0+build"x')

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == '1.0.0+build"x'


def test_patch_errors_when_the_document_has_no_version_key(tmp_path):
    """A schema with no top-level version is a hard error, not a silent no-op."""
    _write_schema(tmp_path, '{\n  "schema_version": 2,\n  "name": "demo"\n}\n')

    with pytest.raises(ReleaseValidationError) as exc:
        _patch_schema_version(str(tmp_path), "1.2.3")
    assert "no top-level 'version' key" in str(exc.value)


def test_patch_errors_on_a_non_canonically_encoded_schema(tmp_path):
    """A compact document is not something a strictcli dump wrote.

    The patch is pinned to the canonical encoding, so it refuses rather than
    silently leaving the version alone -- and the message names the second
    possibility, since "no version key" would be a misdiagnosis here.
    """
    _write_schema(tmp_path, '{"schema_version": 2, "version": "0.1.0"}\n')

    with pytest.raises(ReleaseValidationError) as exc:
        _patch_schema_version(str(tmp_path), "1.2.3")
    assert "canonical encoding" in str(exc.value)


def test_patch_errors_when_the_schema_file_is_missing(tmp_path):
    """A dump that produced no file is a failed dump, reported as one."""
    with pytest.raises(ReleaseValidationError) as exc:
        _patch_schema_version(str(tmp_path), "1.2.3")
    assert "does not exist" in str(exc.value)
