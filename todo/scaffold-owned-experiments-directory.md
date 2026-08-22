# A scaffold-owned experiments directory convention, fleet-wide

## Problem

The fleet's working convention keeps experiments and temporary probes
inside the project directory (not /tmp). Separately, projects grow
repo-integrity guards implemented as filesystem walks — AST sweeps over
every source file under the repo root — which deliberately ignore
.gitignore so that misplaced production code cannot hide in an ignored
directory. The two collide: an agent's in-project scratch (including
throwaway git repositories used to probe tool behavior, whose contents
can match a guard's refused shapes) turns a guard test red. This
happened in practice in a consumer repo; the interim fix is "put
guard-triggering scratch outside the repo," which weakens the
keep-it-in-the-project convention.

## Proposal

The scaffold owns a per-repo experiments directory convention:

- A canonical name at the repo root (e.g. `experiments/` or
  `.experiments/` — decide once, fleet-wide).
- `rlsbl scaffold` adds the gitignore entry (contents ignored) and a
  small committed marker file (a README or .gitkeep-style stub stating
  the directory's purpose and that tools ignore it), so the directory
  is discoverable and its convention self-documenting.
- The convention's other half is consumer-side but standardized:
  project guards and sweeps exclude the directory by its root-relative
  name, exactly like their existing docs/scripts/testdata exclusions.
  (Possibly an rlsbl check that a repo's declared guard skip-lists
  include it, if a machine-readable declaration exists; otherwise the
  convention is documented and each guard adds one entry.)

Then experiments live in the project again with zero guard collisions
by construction, and every repo has one canonical scratch location
instead of ad-hoc gitignored directories.

## Accepted cost

An excluded directory is a place misplaced production code goes
unscanned — the same accepted class as the existing named exclusions,
mitigated by the exclusion being root-relative and single-named.

## Effort

Small in the scaffold (gitignore entry + marker in the template set);
per-consumer one skip-list line as repos adopt.
