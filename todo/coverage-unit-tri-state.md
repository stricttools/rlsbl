# `coverage_unit` behaves as a tri-state

## Context

Found during a code audit (2026-07) of changelog coverage configuration.

## Problem

`coverage_unit` is documented as a two-value choice (`"commit"` or `"changeset-file"`), but
the implementation actually distinguishes three states: `"commit"`, `"changeset-file"`, and
*absent* — with the absent state taking a code path that is not identical to either explicit
value (or accepting arbitrary other strings without complaint). A config key whose absence
means something subtly different from its documented default is an implicit-default bug: the
same documented configuration can behave differently depending on whether the default was
spelled out.

## Suggested fix

1. Write tests pinning the behavior of all three states (absent / `"commit"` /
   `"changeset-file"`) plus an invalid string, and compare — make the divergence visible first.
2. Collapse to a true two-state: absent means exactly `"commit"` (byte-for-byte the same code
   path), and any other value is a hard error naming the two accepted values.
3. If the absent-state divergence turns out to be load-bearing for some consumer, that is a
   third mode that must be named and documented, not left as an accident.

## Affected area

Changelog coverage configuration (`coverage_unit`) and every check that branches on it.

## Effort

Small-medium: the fix is mechanical once the three current behaviors are pinned by tests.
