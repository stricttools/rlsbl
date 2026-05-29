# GoTarget.template_vars() broken: project_root not passed by callers

## Problem

`rlsbl scaffold` fails for Go projects with:

```
GoTarget.template_vars() missing 1 required positional argument: 'project_root'
```

The `project_root` parameter was added to `template_vars()` and `shared_template_mappings()` on GoTarget, ZigTarget, and DockerTarget, then made required — but the callers were not updated to pass it.

## Root cause

Two commits introduced the bug:

1. `268e794` — added `project_root` as optional (`project_root=None`) to `template_vars()` and `shared_template_mappings()` on Go/Zig/Docker targets.
2. `3952896` — made `project_root` required on all those methods but did not update all callers.

This creates a signature mismatch with the protocol/base class:

| Layer | `template_vars` | `shared_template_mappings` |
|-------|-----------------|---------------------------|
| Protocol (`protocol.py:65`) | `(self, dir_path)` | `(self)` |
| BaseTarget (`base.py:36`) | `(self, dir_path)` | `(self)` |
| GoTarget (`go.py:245`) | `(self, dir_path, project_root)` | `(self, project_root)` |
| ZigTarget (`zig.py:103`) | `(self, dir_path, project_root)` | `(self, project_root)` |
| DockerTarget (`docker.py:122`) | `(self, dir_path, project_root)` | inherits base |

## Broken call sites

### template_vars() — 5 callers missing project_root:

1. `commands/init_cmd.py:957` — `reg.template_vars(".")`
2. `commands/init_cmd.py:1326` — `primary_target.template_vars(...)` inside `_merge_template_vars()`
3. `commands/init_cmd.py:1331` — `target.template_vars(...)` inside `_merge_template_vars()`
4. `commands/status.py:45` — `reg.template_vars(target_path)`
5. `commands/record_gif.py:34` — `registry_module.template_vars(first_path)`

### shared_template_mappings() — 2 callers missing project_root:

1. `commands/init_cmd.py:976` — `reg.shared_template_mappings()`
2. `commands/init_cmd.py:1497` — `reg.shared_template_mappings()`

## Targets affected

Go, Zig, Docker. All other targets (npm, pypi, cargo, etc.) use the base class signature and are unaffected.

## Fix

Update all 7 call sites to pass `project_root`. Thread `project_root` into `_merge_template_vars()` (line 1313) and the `record_gif` helper. `project_root` is already available in each enclosing `run_cmd()` function — it just needs to be passed through.

## Effort

Small — 7 call sites, mostly adding one argument. The `_merge_template_vars` function needs a new parameter threaded through, and the `record_gif` helper needs `project_root` added to its parameter list.
