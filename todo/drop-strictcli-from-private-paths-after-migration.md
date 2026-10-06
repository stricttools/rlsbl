# Drop `.strictcli` from the private upload paths once no repository still has it

## Context

`rlsbl/private_paths.py` lists `.strictcli` in `PRIVATE_DIRS`, so the
directory never reaches a package upload. The family is moving the contents of
`.strictcli/` (the command-line schema and test-coverage files strictcli
writes) into `.strictmetadata/.cli-schema/` and
`.strictmetadata/.cli-test-coverage/`, migrated with `selfdoc layout migrate`.
The first migration covers only strictcli, rlsbl, selfdoc, saferm, and
safegit; other family repositories keep `.strictcli/` until they migrate.

While any repository still holds `.strictcli/`, the entry protects it from
publishing private files, which would be permanent on registries. Once none
does, the entry is a dead reference to a retired layout.

## Work

1. Check every family repository under `~/Projects/stricttools/tools/` for a
   `.strictcli/` directory; if any remains, this todo is not ready.
2. Remove `.strictcli` from `PRIVATE_DIRS` and from any test or doc naming it
   as private, with a changelog entry.
3. Run the falsified-texts sweep for `.strictcli` in rlsbl.

## Effort

Small, once every repository has migrated.
