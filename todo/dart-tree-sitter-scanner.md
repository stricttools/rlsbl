# Replace Dart import scanner regex with tree-sitter-dart

## Problem

The Dart import scanner was designed to use tree-sitter-dart for parsing imports, but the implementation used regex (`import 'package:foo/'` pattern) instead. This works for the simple case but won't handle commented-out imports or string literals containing import-like patterns.

## What's needed

- Add `tree-sitter-dart` as a dependency
- Replace the regex-based scanner in the Dart import extraction module with a tree-sitter-based parser
- Match the approach used by the Python import scanner (which uses tree-sitter)
- Add test cases for edge cases: commented-out imports, multi-line imports, string literals with import-like content

## Effort

Small-medium.
