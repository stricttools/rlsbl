"""The release's schema-version stamp must preserve every other byte.

`rlsbl release run` writes a strictcli consumer's help document
(`<app> help --json`) to `.strictcli/schema.json` with the new version in it. The patch used to be
`json.dumps(json.load(f), indent=2)`, which re-encodes the WHOLE document with
Python's own defaults -- and strictcli writes that file in its own canonical
encoding (schema v2, contract 25.8): raw UTF-8 with `ensure_ascii=False`, no
HTML escaping, canonical floats, two-space indent, one trailing newline.

The two encodings disagree on real content. Every non-ASCII character in any
help text -- and rlsbl's own help is full of em dashes -- came back out as a
`\\uXXXX` escape, so every consumer release rewrote its schema file into
something no strictcli implementation would ever have written, and the next
dump reverted it. The stamp is textual now: one key's line, nothing else.
"""

import json

import pytest

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    _stamp_schema_version,
)

_PATH = ".strictcli/schema.json"


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


def test_stamp_rewrites_only_the_version_line():
    """Every byte but the version value survives the stamp."""
    after = _stamp_schema_version(_CANONICAL_SCHEMA, "9.9.9", _PATH)

    expected = _CANONICAL_SCHEMA.replace('"version": "0.1.0"', '"version": "9.9.9"', 1)
    assert after == expected


def test_stamp_preserves_non_ascii_and_unescaped_html():
    """The em dash stays an em dash and `&` stays `&`."""
    after = _stamp_schema_version(_CANONICAL_SCHEMA, "2.0.0", _PATH)

    assert "an em dash & an ampersand" in after
    assert "\\u2014" not in after
    assert "\\u0026" not in after


def test_stamp_preserves_the_canonical_float_form():
    """`1e-7` is the canonical float form; Python's repr writes `1e-07`."""
    after = _stamp_schema_version(_CANONICAL_SCHEMA, "2.0.0", _PATH)

    assert '"threshold": 1e-7' in after
    assert "1e-07" not in after


def test_stamp_leaves_a_nested_version_key_alone():
    """Only the top-level `version` is the document's version."""
    after = _stamp_schema_version(_CANONICAL_SCHEMA, "3.1.4", _PATH)

    data = json.loads(after)
    assert data["version"] == "3.1.4"
    assert data["commands"][0]["flags"][0]["name"] == "version"
    assert '"name": "version"' in after


def test_stamp_escapes_the_new_version_as_a_json_string():
    """The replacement value is written as a JSON string literal, not spliced raw."""
    after = _stamp_schema_version(_CANONICAL_SCHEMA, '1.0.0+build"x', _PATH)

    data = json.loads(after)
    assert data["version"] == '1.0.0+build"x'


def test_stamp_errors_when_the_document_has_no_version_key():
    """A schema with no top-level version is a hard error, not a silent no-op."""
    with pytest.raises(ReleaseValidationError) as exc:
        _stamp_schema_version(
            '{\n  "schema_version": 2,\n  "name": "demo"\n}\n', "1.2.3", _PATH,
        )
    assert "no top-level 'version' key" in str(exc.value)


def test_stamp_errors_on_a_non_canonically_encoded_schema():
    """A compact document is not something a strictcli dump wrote.

    The stamp is pinned to the canonical encoding, so it refuses rather than
    silently leaving the version alone -- and the message names the second
    possibility, since "no version key" would be a misdiagnosis here.
    """
    with pytest.raises(ReleaseValidationError) as exc:
        _stamp_schema_version(
            '{"schema_version": 2, "version": "0.1.0"}\n', "1.2.3", _PATH,
        )
    assert "canonical encoding" in str(exc.value)


def test_stamp_replaces_an_empty_version():
    """A Go program whose version is injected by ldflags reports `""` under
    `go run`; the committed document still carries the release version."""
    document = '{\n  "schema_version": 2,\n  "version": "",\n  "commands": []\n}\n'

    after = _stamp_schema_version(document, "1.4.0", _PATH)

    assert after == document.replace('"version": ""', '"version": "1.4.0"')
