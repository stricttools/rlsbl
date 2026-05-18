# Detect Mix project shape for hex (beam) install command

## Context

`HexTarget.dev_install_command` currently returns `mix deps.get` for both global and venv modes regardless of the project type. This is the conservative choice -- it always works -- but it doesn't match Elixir CLI conventions for a project that ships an escript binary, where the user-visible expectation is `mix escript.install`.

## Proposed detection

Read `mix.exs` for an `escript: [main_module: ...]` configuration block in the project's keyword list:

- If present, use `mix escript.install` for `--global` mode (installs the escript binary onto `$PATH` via `~/.mix/escripts`).
- Otherwise, keep `mix deps.get` (library/app projects without a binary entry point).
- `--venv` mode keeps `mix deps.get` regardless -- the BEAM ecosystem has no per-project venv concept.

Detection should be a simple substring/regex check on the `mix.exs` source. No need to evaluate the file; the pattern is conventional enough that a regex like `escript:\s*\[` is sufficient. Fall back to `mix deps.get` if `mix.exs` can't be read.

## Why deferred

- No Elixir consumer in the rlsbl user base to validate the behaviour against.
- The renamed-target work (`hex` -> `beam`) is also pending and would move the target file from `hex.py` to `beam.py`. Making the change here would create churn during the rename.
- Conservative current behaviour is correct (just suboptimal), so there's no urgency.

## Dependencies

Should wait for `todo/target-rename-split.md` to land first, since the target module moves from `rlsbl/targets/hex.py` to `rlsbl/targets/beam.py` (with `hex` becoming a sub-mode alongside `gleam`/`erlang`). Implementing the detection before the rename would either need to be redone or would block the rename PR.

## Affected files

- `rlsbl/targets/hex.py` (or `rlsbl/targets/beam.py` post-rename) -- override `dev_install_command`
- `tests/test_dev_install.py` -- two new cases (escript present -> `mix escript.install`; absent -> `mix deps.get`)

## Effort

Small. One method override plus a couple of test cases. The bulk of the cost is the rename it depends on.
