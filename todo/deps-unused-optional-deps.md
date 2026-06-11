# deps-unused should distinguish hard vs optional dependencies

## Problem

After the try/except ImportError filtering (v0.66.3), imports inside try/except blocks are invisible to deps-unused. A package declared as a hard dependency but only imported inside try/except is flagged as "declared but never imported." This is correct for hard deps (contradictory to declare hard + import optionally), but wrong for optional/extras dependencies declared via `[project.optional-dependencies]` in pyproject.toml.

## Proposed solution

Read `[project.optional-dependencies]` from pyproject.toml. For packages listed there, try/except imports SHOULD count as usage (they are intentionally optional). For packages in `[project.dependencies]` (hard deps), try/except imports should NOT count (current behavior — contradictory declaration).

## Effort

Medium. Requires reading optional-dependencies from pyproject.toml, threading the distinction through the import scanner, and adjusting deps-unused logic.
