# Fleet test-isolation program (master record)

The program record for extracting this repo's test guards into fleet infrastructure.
Provenance: `[%%]`-marked decisions were adopted from recommendations (freely reversible,
never to be cited as deliberate intent); unmarked were deliberate user rulings.

**BLOCKING INPUT: the new package's name and license (user only).** Nothing may be created on
any registry or as a repo until both are given.

## Decisions

- Delivery `[%%]`: hybrid — the in-process floor becomes a **published pytest plugin** (pytest11
  entry point) living in a **new repo** which also ships a small **Go helper module as a second
  releasable** (one name covers both languages); the sandbox runner + CI steps ship as scaffold
  templates; adoption is enforced by a check (hard once a repo has adopted).
- DB tests `[%%]`: the **in-sandbox ephemeral Postgres cluster** is the single mechanism
  (locally and in CI). Once landed, DSN refusal guards, tier branching, testcontainers usage,
  and hand-written CI Postgres service blocks get deleted fleet-wide. Per-test template forks
  (host-blind injection) are a later opportunistic upgrade per fixture.
- Socket guard: hand-rolled (~50 lines) — pytest-socket verified unfit (host-only granularity;
  no host:port or unix-path allowlists, which DB tests need).
- Bare-run refusal for Go/node: wrapper-script + release-time check (no runtime hook exists);
  threshold stays 50 for Python.

## The floor (extraction source: `tests/conftest.py` lines ~48-385)

Env-poisoning floor (48-139: throwaway HOME/XDG, throwaway git config, transport lockdown,
credential stripping — the Go-cache/PYTHONUSERBASE carve-outs become opt-in per-project
preservation config); bare-run threshold (149-207, incl. the exact xdist controller/worker
topology); TMPDIR-inside-repo refusal (232-269; plugin uses `config.rootpath`); autouse chdir
+ escape marker (272-290); the Popen push guard (292-373). Sandbox detection = a configurable
env-var name (currently `RLSBL_TEST_SANDBOX`, set by `scripts/test.sh`). Per-project config via
`[tool.pytest.ini_options]`; safety keys (socket stance, allowlists) are REQUIRED — no implicit
defaults. New: the socket guard (default network-off; host:port + unix-socket-path allowlists;
loopback stance explicit). The remaining ~750 conftest lines are repo-specific and stay.

## The runner template + in-sandbox Postgres

- Generalize `scripts/test.sh` into a scaffold shared template: parameterized inner command
  (pytest / go test / node), per-ecosystem cache binds + pre-warm hooks, the bwrap preflight
  probe, the handshake env var. Parameters live in `.rlsbl` config so the template stays
  compatible with regenerate-only scaffolding (ledger 7.1).
- Cluster helper `[%%]`: `initdb --no-sync` into tmpfs; `fsync=off`; **socket on a short
  dedicated tmpfs path — the 107-byte unix-socket path limit is hard (probe-verified)**; one
  shared cluster + ephemeral per-test databases (the pgdesign testdb model — no per-worker
  sockets needed); extensions load offline from the RO `/usr` bind (locally verified: PG 18.3,
  pgvector, pg_partman; startup <1s). DSN exported under per-project env names (compatible with
  strictcli's connection-env kind). CI: install postgres + extension packages on the runner
  host; the container-based provisioning gets deleted.
- The package-fetch boundary is a SEPARATE named problem: toolchain lanes that fetch packages
  (gradle/npm) stay warm-cached or CI-only — never inside `--unshare-net`.

## Adoption + enforcement

- Adoption check: plugin in dev deps + runner present + CI uses it; registers in
  `data/checks.toml` + `checks/project.py` + `CHECK_TARGETS` (coordinate: hot files shared with
  the in-flight state-layer program). Hard-errors only once a repo has adopted.
- Fleet rollout `[%%]`: risk-ordered — highest-credential-exposure consumers first; the
  strictcli classification sweep rides the same visits where still unfinished; Go repos get the
  runner + the shared env-hygiene module (until published, repos vendor the helper in
  `internal/testutil` — swap to the module import when it exists).
- One consumer's DB extension can likely be **eliminated** instead of provisioned: PG 18 ships
  native `uuidv7()` `[%%]` — verify that consumer's production PG is ≥18 first; fallback is an
  offline source-build.
- Close-out: Go analyzer lint (chdir-without-restore; env-passthrough test spawns); extend the
  push-guard's pattern coverage beyond `git push` (gh/npm mutating verbs); then flip the
  adoption check hard fleet-wide.
