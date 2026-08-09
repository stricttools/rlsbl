# Native root-member support (sentinel name instead of hand-picked)

## Context

A workspace root member (path = ".") currently needs a hand-picked name; one fleet
monorepo's root member had none and got named "root" ad hoc (it now appears in
workspace.toml, router job keys, and gate regexes). Root members are the residual-claimant
norm in the redesign ledger (§1.4/§2.1), so every monorepo eventually hits this.

## Problem

The name is load-bearing across generated artifacts, yet nothing owns its choice: agents
improvise, spellings will drift per repo, and renames later mean workspace + router +
gate-regex churn.

## Direction (user, 2026-08-09)

rlsbl should support root members natively — an empty or sentinel name for path = "."
members, with display/job-key/gate derivations owned by the tool (one convention
everywhere). Design questions: sentinel spelling; migration for the existing hand-named
instances; interaction with tag_format {name} and releasable naming.

## Effort

Small-medium design + mechanical adoption.
