# `rlsbl commit` re-serializes `.strictcli/schema.json`, churning against Go dumps

## Problem (observed 2026-08-07)

`rlsbl commit` runs the schema through Python's json with `ensure_ascii=True`
before committing, while a Go-written `--dump-schema` file escapes `<`/`>` as
`<`-style and leaves em-dashes raw. Identical content therefore differs
byte-for-byte depending on which tool wrote it last, producing spurious dirty
files and an extra commit round-trip every time a Go project's schema passes
through `rlsbl commit` (and vice versa).

## Fix directions

- (a) `rlsbl commit` stops re-serializing: commit the file's bytes as-is
  (it has no reason to parse a file it is only staging). Simplest and most
  correct — the dumping tool owns the canonical form.
- (b) If re-serialization exists for a reason (validation?), validate without
  rewriting, or match the source language's serialization exactly (fragile —
  three languages, three serializers).

Recommend (a); red-green with a Go-shaped schema fixture asserting
byte-identity through `rlsbl commit`.

## Effort

Small.
