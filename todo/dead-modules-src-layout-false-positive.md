# dead-modules reports every module of a src-layout package as dead

## Context

`rlsbl check --name dead-modules` derives Python module names from the path
relative to the project root. For a package laid out as `src/<pkg>/`, that
yields names like `src.<pkg>.app`, while every import in the tree says
`<pkg>.app`. The prefix comparison therefore never matches, and each module
that is only imported from inside the package is reported as "not imported by
any other module".

## Problem

A conventional src-layout project cannot get a clean `rlsbl check --all`
without excluding its real modules in `.rlsbl/dead-modules.toml` with a
reason that is not true. The check is a false positive for the whole
layout, not for individual files.

## Solutions

1. Resolve module names through the declared package roots: read
   `[tool.hatch.build.targets.wheel] packages`, `[tool.setuptools]`
   `package-dir`, or fall back to detecting a `src/` directory that contains
   packages, and strip that root before forming the dotted name. Most
   correct; matches how the interpreter sees the modules.
   - Pros: no per-project configuration, fixes every src-layout repo at once.
   - Cons: needs a small manifest reader per build backend.
2. Accept a `module_roots` key in `.rlsbl/config.json` listing directories to
   strip. Simple, but a new input surface that every src-layout project must
   remember to set, and the failure mode without it stays a false positive.

Option 1 is the recommendation.

## Affected files

- the dead-modules check implementation and its tests
- docs for the check

## Effort

Small: one name-resolution function plus fixtures for a src-layout project
and a flat-layout project.
