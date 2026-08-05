"""Watch command that polls GitHub Actions CI workflow runs for a given commit SHA and reports pass, fail, or in-progress status."""

import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..ci_checks import verify_project_ci_ran
from ..utils import require_tool, run, run_gh
from .. import effects


def _open_url(url):
    """Open a URL in the default browser. Non-fatal if unavailable."""
    try:
        if sys.platform == "darwin":
            effects.run(["open", url], timeout=5, capture_output=True)
        else:
            effects.run(["xdg-open", url], timeout=5, capture_output=True)
    except Exception:
        pass


def _release_url(repo_slug):
    """Try to find the latest release tag and return its GitHub URL. Returns None on failure."""
    if not repo_slug:
        return None
    try:
        raw = run_gh(["release", "list", "--limit", "1", "--json", "tagName", "-q", ".[0].tagName"])
        if raw:
            return f"https://github.com/{repo_slug}/releases/tag/{raw}"
    except Exception:
        pass
    return None


def _notify(title, body, url=None):
    """Send a desktop notification. If url is provided, opens it only when the user clicks the notification action."""
    try:
        if sys.platform == "darwin":
            escaped_title = title.replace('"', '\\"')
            escaped_body = body.replace('"', '\\"')
            effects.run(
                ["osascript", "-e",
                 f'display notification "{escaped_body}" with title "{escaped_title}"'],
                timeout=5, capture_output=True,
            )
        elif require_tool("notify-send", fatal=False):
            cmd = ["notify-send", "-u", "normal"]
            if url:
                cmd += ["--action", "open=Open"]
            cmd += [title, body]
            result = effects.run(cmd, timeout=120, capture_output=True, text=True)
            if url and result.stdout.strip() == "open":
                _open_url(url)
    except Exception:
        pass


# Failure classification for retry gating.
#
# A CI/Publish run that failed for a DETERMINISTIC reason (a test failure baked
# into the tag, a compile error, a config/validation error, a workflow syntax
# error, a missing-secret/auth denial) will fail identically on retry -- retrying
# it wastes a full CI run and delays diagnosis. Before retrying, we fetch the
# tail of the failing step's log and match it against these signatures.
#
# Both lists are module-level tuples of compiled, case-insensitive regexes so
# new signatures can be appended without touching the classification logic.
# Add deterministic signatures here as new blind-retry-wasted cases are observed.

# Last N lines of the failing step log to fetch and classify.
_LOG_TAIL_LINES = 100
# Timeout (seconds) for the `gh run view --log-failed` fetch. Matches the
# 30s budget used for the retry dispatch below (external calls must be bounded).
_LOG_FETCH_TIMEOUT = 30

# Deterministic: the failure will recur identically on retry -> never retry.
# MULTILINE so ^-anchored patterns match at the start of any log line, not
# just the start of the whole tail.
_DETERMINISTIC_SIGNATURES = tuple(
    re.compile(p, re.IGNORECASE | re.MULTILINE) for p in (
        # --- Test-suite failures (pytest / go test / npm/jest) ---
        r"={3,}\s*\d+\s+failed",          # pytest summary: "=== 1 failed ..."
        r"short test summary info",       # pytest failure section header
        r"^FAILED\s+\S",                  # pytest per-test failure line
        r"--- FAIL:",                     # go test failure
        r"^FAIL\b",                       # go test package failure
        r"\[build failed\]",              # go test build failure
        r"Tests:.*\bfailed",              # jest summary: "Tests: 1 failed, ..."
        r"npm ERR!\s+Test failed",        # npm test script failure
        # --- Compile / build errors ---
        r"compilation (failed|error)",
        r"\bbuild failed\b",
        r"error\[E\d+\]",                # rust compiler diagnostic code
        r"\bSyntaxError\b",
        r"cannot find (module|package)",
        r"undefined reference to",
        r"undefined:\s",                  # go "undefined: Foo"
        # --- Config / validation errors ---
        r"strictcli",                    # strictcli registration/validation error
        r"registration error",
        r"\bConfigError\b",
        r"\bValidationError\b",
        r"invalid configuration",
        r"goreleaser\b.*\b(error|invalid)",
        r"only configuration files are allowed",  # goreleaser config error
        # --- Workflow / YAML syntax errors ---
        r"Invalid workflow file",
        r"yaml:\s*line\s*\d+",
        r"workflow.*syntax error",
        # --- Missing-secret / auth / permission denials (re-run identically) ---
        r"Input required and not supplied",   # GH Actions missing secret/input
        r"could not read (Username|Password)",
        r"Permission denied",
        r"denied: permission_denied",
        r"authentication failed",
        r"remote: Permission to .* denied",
    )
)

# Transient: infrastructure flake -> retry once (same as historical behavior).
_TRANSIENT_SIGNATURES = tuple(
    re.compile(p, re.IGNORECASE | re.MULTILINE) for p in (
        # --- Rate limits ---
        r"rate limit",
        r"\b429\b",
        r"Too Many Requests",
        # --- Network / DNS / TLS ---
        r"i/o timeout",
        r"connection (reset|refused|timed out)",
        r"network is unreachable",
        r"temporary failure in name resolution",
        r"TLS handshake timeout",
        r"\bdial tcp\b",
        # --- 5xx server errors ---
        r"HTTP 5\d\d",
        r"5\d\d\s+(Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out)",
        r"internal server error",
        # --- Runner-lost / cancelled infrastructure ---
        r"The runner has received a shutdown signal",
        r"lost communication with the server",
        r"The operation was canceled",
        r"received request to (deprovision|cancel)",
    )
)


def _classify_failure(log_text):
    """Classify a CI failure log tail to decide whether a retry is worthwhile.

    Returns one of:
      - "deterministic": a signature indicating the failure recurs identically
        on retry (test failures, compile/build errors, config/validation errors,
        workflow syntax errors, missing-secret/auth denials). Never retry.
      - "transient": an infrastructure-flake signature (network timeouts, 5xx,
        rate limits, runner-lost/cancelled). Retry once.
      - "unknown": no signature matched. Treated by the caller as transient
        (retry once) -- this DEFAULT preserves the historical blind-retry
        behavior for failures we don't yet recognize, rather than suppressing a
        retry that might have succeeded.

    Deterministic signatures are checked before transient ones so a log that
    contains both a hard error and incidental network chatter is treated as
    deterministic (the hard error is the real cause).
    """
    if not log_text:
        return "unknown"
    for pat in _DETERMINISTIC_SIGNATURES:
        if pat.search(log_text):
            return "deterministic"
    for pat in _TRANSIENT_SIGNATURES:
        if pat.search(log_text):
            return "transient"
    return "unknown"


def _fetch_failure_log(run_id, config=None):
    """Fetch the tail of the failing step's log for a run via gh.

    Runs `gh run view <id> --log-failed` (through run_gh, so GH_REPO resolution
    and thread-safe env handling apply) with a bounded timeout, and returns the
    last _LOG_TAIL_LINES lines joined as a single string. Propagates any
    exception from the gh call so the caller can emit a loud fallback note.
    """
    raw = run_gh(["run", "view", str(run_id), "--log-failed"],
                 config=config, timeout=_LOG_FETCH_TIMEOUT)
    lines = raw.splitlines()
    return "\n".join(lines[-_LOG_TAIL_LINES:])


def _retry_workflow(workflow_name, repo_slug, label, failed_run_id):
    """Re-run a failed workflow run in place once and watch the new attempt.

    Uses `gh run rerun <failed_run_id>` -- a FULL rerun (not --failed) of the
    SAME run id. GitHub re-executes the run as a new attempt on the failed
    run's original commit, so (a) no duplicate check-run is created (a fresh
    `gh workflow run` dispatch would poison the publish gate) and (b) the retry
    runs on the failed commit rather than branch HEAD.

    Because the run id is unchanged, there is no dispatched-run-identification
    dance: we simply watch failed_run_id for its new attempt's conclusion.

    Returns a result dict with name, passed, and run_id.
    Returns None if the rerun could not be triggered.
    """
    print(f"rlsbl: {label}: [{workflow_name}] CI failed, retrying once...", file=sys.stderr)
    try:
        run_gh(["run", "rerun", str(failed_run_id)], timeout=30)
    except Exception as exc:
        print(f"rlsbl: {label}: [{workflow_name}] retry trigger failed: {exc}", file=sys.stderr)
        return None

    retry_id = str(failed_run_id)
    print(f"rlsbl: {label}: [{workflow_name}] watching retry run {retry_id}...", file=sys.stderr)

    try:
        run_gh(["run", "watch", retry_id, "--exit-status"], timeout=3600)
        print(f"rlsbl: {label}: [{workflow_name}] retry passed", file=sys.stderr)
        return {"name": workflow_name, "passed": True, "run_id": retry_id}
    except subprocess.CalledProcessError:
        print(f"rlsbl: {label}: [{workflow_name}] retry also failed", file=sys.stderr)
        if repo_slug:
            print(f"rlsbl: https://github.com/{repo_slug}/actions/runs/{retry_id}",
                  file=sys.stderr)
        # Print the failing-step log tail alongside the URL so the operator
        # gets an immediate diagnosis. Wrapped in try/except so a broken fetch
        # never breaks the return path (mirrors the loud-note fallback in
        # _watch_single_run).
        try:
            log_tail = _fetch_failure_log(retry_id)
            if log_tail.strip():
                print(f"rlsbl: {label}: [{workflow_name}] retry failure log tail:",
                      file=sys.stderr)
                print(log_tail, file=sys.stderr)
        except Exception as exc:
            print(f"rlsbl: {label}: [{workflow_name}] could not fetch retry "
                  f"failure logs: {exc}", file=sys.stderr)
        return {"name": workflow_name, "passed": False, "run_id": retry_id}
    except subprocess.TimeoutExpired:
        print(f"rlsbl: {label}: [{workflow_name}] retry timed out after 1h", file=sys.stderr)
        return {"name": workflow_name, "passed": False, "run_id": retry_id}
    except Exception as exc:
        print(f"rlsbl: {label}: [{workflow_name}] retry error: {exc}", file=sys.stderr)
        return {"name": workflow_name, "passed": False, "run_id": retry_id}


def _watch_single_run(ci_run, label, repo_slug, retried_lock=None,
                      retried_workflows=None, timeout=3600):
    """Watch a single CI run. Returns a dict with name, passed, and run_id.

    When retried_lock and retried_workflows are provided, deduplicates retries
    so that only one retry is dispatched per workflow name even when multiple
    runs from the same workflow fail concurrently.

    Retries are in-place reruns (`gh run rerun`) that reuse the failed run's
    own id, so no cross-thread run-id bookkeeping is needed: the late re-poll
    recognizes the reran run by its unchanged id.
    """
    run_id = str(ci_run["databaseId"])
    workflow_name = ci_run.get("name", f"run {run_id}")

    try:
        # gh run watch blocks until the run completes;
        # --exit-status makes it exit 1 on failure; check=True raises
        # CalledProcessError so we can distinguish pass from fail
        run_gh(["run", "watch", run_id, "--exit-status"], timeout=timeout)
        msg = f"rlsbl: {label}: [{workflow_name}] passed"
        print(msg, file=sys.stderr)
        return {"name": workflow_name, "passed": True, "run_id": run_id}
    except subprocess.CalledProcessError:
        msg = f"rlsbl: {label}: [{workflow_name}] FAILED"
        print(msg, file=sys.stderr)
        if repo_slug:
            print(f"rlsbl: https://github.com/{repo_slug}/actions/runs/{run_id}",
                  file=sys.stderr)

        # Fetch the failing step's log tail and classify the failure before
        # retrying. Deterministic failures (test/compile/config/auth errors)
        # recur identically on retry, so we skip the retry entirely. The tail
        # is ALWAYS printed on failure -- retried or not -- so the operator
        # gets an immediate diagnosis instead of just a run URL.
        classification = "unknown"
        try:
            log_tail = _fetch_failure_log(run_id)
            if log_tail.strip():
                print(f"rlsbl: {label}: [{workflow_name}] failure log tail:",
                      file=sys.stderr)
                print(log_tail, file=sys.stderr)
            classification = _classify_failure(log_tail)
        except Exception as exc:
            # A broken gh must not break watching -- fall back to the historical
            # blind retry. This is NOT a silent fallback: the note is loud so the
            # operator knows classification was skipped and why.
            print(f"rlsbl: {label}: [{workflow_name}] could not fetch failure "
                  f"logs: {exc}; retrying without classification", file=sys.stderr)
            classification = "unknown"

        if classification == "deterministic":
            print(f"rlsbl: {label}: [{workflow_name}] deterministic failure "
                  f"detected; not retrying (it would fail identically)",
                  file=sys.stderr)
            return {"name": workflow_name, "passed": False, "run_id": run_id}

        # Auto-retry once before reporting failure (deduplicated by workflow name).
        # Reached for transient and unknown classifications (unknown -> retry
        # preserves the historical behavior for unrecognized failures). The
        # retry is an in-place rerun of this run id, so it needs no branch.
        should_retry = True
        if retried_lock is not None and retried_workflows is not None:
            with retried_lock:
                if workflow_name in retried_workflows:
                    should_retry = False
                else:
                    retried_workflows.add(workflow_name)
        if should_retry:
            retry_result = _retry_workflow(workflow_name, repo_slug, label, run_id)
            if retry_result is not None:
                return retry_result
        return {"name": workflow_name, "passed": False, "run_id": run_id}
    except subprocess.TimeoutExpired:
        # NOT a failure: the local wait ran out, the run itself may still be
        # going. Marked so the caller can report "unresolved" instead of the
        # deterministic-failure remedy (see CI_TIMEOUT in wait_for_ci_green).
        msg = f"rlsbl: {label}: [{workflow_name}] timed out after {timeout}s"
        print(msg, file=sys.stderr)
        return {"name": workflow_name, "passed": False, "timed_out": True,
                "run_id": run_id}
    except Exception as exc:
        msg = f"rlsbl: {label}: [{workflow_name}] error: {exc}"
        print(msg, file=sys.stderr)
        return {"name": workflow_name, "passed": False, "run_id": run_id}


def _watch_runs(runs, label, repo_slug, retried_lock=None, retried_workflows=None,
                timeout=3600):
    """Watch all runs in parallel. Returns list of result dicts.

    retried_lock and retried_workflows may be passed in so that retry
    deduplication state is shared across multiple _watch_runs calls (initial
    watch + late re-poll watch). Fresh state is created when omitted.

    Single-run pools deliberately go through the same thread-pool path so
    every run participates in the shared retry-dedup machinery.
    """
    if retried_lock is None:
        retried_lock = threading.Lock()
    if retried_workflows is None:
        retried_workflows = set()

    results = []
    with ThreadPoolExecutor(max_workers=len(runs)) as executor:
        futures = {
            executor.submit(_watch_single_run, ci_run, label, repo_slug, retried_lock,
                            retried_workflows, timeout): ci_run
            for ci_run in runs
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                # Should not happen since _watch_single_run catches all exceptions,
                # but guard against unexpected failures in the future machinery
                ci_run = futures[future]
                workflow_name = ci_run.get("name", f"run {ci_run['databaseId']}")
                print(f"rlsbl: {label}: [{workflow_name}] thread error: {exc}",
                      file=sys.stderr)
                results.append({"name": workflow_name, "passed": False})
    return results


# Keywords that indicate a publish/deploy workflow (case-insensitive match)
_PUBLISH_KEYWORDS = ("publish", "deploy", "release")


def _repo_root():
    """Best-effort git toplevel; falls back to cwd when not in a git repo."""
    try:
        return run("git", ["rev-parse", "--show-toplevel"])
    except Exception:
        return os.getcwd()


def _has_publish_workflow_on_disk():
    """Check if any .github/workflows file looks like a publish workflow.

    Resolved from the git repo root (not cwd) so monorepo package-dir
    invocations still find the repo-level workflow files.
    """
    workflow_dir = os.path.join(_repo_root(), ".github", "workflows")
    if not os.path.isdir(workflow_dir):
        return False
    for filename in os.listdir(workflow_dir):
        name_lower = filename.lower()
        if any(kw in name_lower for kw in _PUBLISH_KEYWORDS):
            return True
    return False


def _is_publish_workflow(name):
    """Return True if the workflow name matches a publish/deploy/release pattern."""
    return any(kw in name.lower() for kw in _PUBLISH_KEYWORDS)


def _print_workflow_audit(results):
    """Print a summary of which workflows ran and flag missing publish workflows.

    Returns True if a missing-publish warning was printed (for testability).
    """
    ran_names = [r["name"] for r in results]
    has_publish_run = any(_is_publish_workflow(name) for name in ran_names)
    publish_expected = _has_publish_workflow_on_disk()
    missing_publish = publish_expected and not has_publish_run

    # Print summary table
    print("\nWorkflows:", file=sys.stderr)
    for r in results:
        status = "passed" if r["passed"] else "FAILED"
        print(f"  {r['name']:<20s} {status}", file=sys.stderr)

    if missing_publish:
        print("  (!) Publish workflow exists but did not run", file=sys.stderr)
        print(
            "\nWarning: publish workflow exists but did not trigger for this "
            "commit. The package may not have been published.",
            file=sys.stderr,
        )

    return missing_publish


def _resolve_run_ids(run_ids):
    """Resolve run IDs to run info dicts via gh run view."""
    runs = []
    for rid in run_ids:
        try:
            output = run_gh(["run", "view", str(rid), "--json", "databaseId,name,status,headBranch,workflowName"])
            info = json.loads(output)
            runs.append(info)
        except Exception as e:
            print(f"Error: could not resolve run ID {rid}: {e}", file=sys.stderr)
            sys.exit(1)
    return runs


def poll_runs(commit_sha, max_attempts=30, interval=4):
    """Poll gh run list until at least one run appears.

    Returns a list of run dicts (may be empty if nothing found after all attempts).
    Default timeout is ~120s (30 attempts * 4s interval).
    """
    for _ in range(max_attempts):
        try:
            raw = run_gh(["run", "list", "--commit", commit_sha,
                         "--json", "databaseId,name,status,headBranch,workflowName"])
            parsed = json.loads(raw)
            if parsed:
                return parsed
        except Exception:
            pass
        time.sleep(interval)
    return []


# ---------------------------------------------------------------------------
# In-process CI wait (main-as-candidate release ordering)
# ---------------------------------------------------------------------------

# How long to wait for CI runs to APPEAR for a freshly pushed candidate before
# concluding that the push produced none. Distinct from the CI-completion
# budget (see get_ci_timeout): a queued-but-not-yet-created run is normal for
# a minute or two, a run that never appears at all is a hard error.
CI_DISCOVERY_GRACE_SECONDS = 300
CI_DISCOVERY_INTERVAL = 5

# Discovery is spent INSIDE the total CI budget, so it is capped at half of it:
# whatever the operator declares with --ci-timeout / ci_timeout, at least half
# of it is always left for the runs to actually complete. Without the cap a
# short budget was swallowed whole by the 300s grace and the completion wait
# collapsed to a 1-second sham that reported an immediate "timeout".
CI_DISCOVERY_BUDGET_SHARE = 2

# The shortest completion wait rlsbl will start. Below this the declared budget
# is simply spent, and saying so is more honest than starting a wait that
# cannot conclude.
CI_MIN_COMPLETION_WINDOW = 30

# Verdicts returned by wait_for_ci_green.
CI_GREEN = "green"
CI_RED = "red"
CI_NOT_CONFIGURED = "no-ci"
# The local wait ran out with runs still unresolved. NOT a red verdict: the
# runs may still be in flight, so the remedy is to check their status and
# resume, never the fix-forward-the-code remedy a real failure calls for.
CI_TIMEOUT = "timeout"


def _discovery_budget(timeout, discovery_grace):
    """Clamp the discovery grace so it never eats the whole CI budget.

    Returns ``(effective_grace, clamped)``. At least one poll interval is
    always granted -- a budget too small to poll even once is the operator's
    declaration, not a reason to skip discovery entirely.
    """
    cap = max(CI_DISCOVERY_INTERVAL, int(timeout) // CI_DISCOVERY_BUDGET_SHARE)
    if discovery_grace <= cap:
        return discovery_grace, False
    return cap, True


def _timeout_verdict(results):
    """Aggregate per-run results into a verdict.

    A genuine failure outranks a timeout: if any run definitively failed, the
    answer is known (:data:`CI_RED`) and fix-forward is the right remedy, even
    if a sibling run was still going when the budget ran out.
    """
    if all(r["passed"] for r in results):
        return CI_GREEN
    if any(not r["passed"] and not r.get("timed_out") for r in results):
        return CI_RED
    return CI_TIMEOUT


class CIWaitError(Exception):
    """Raised when the CI wait cannot reach a verdict at all.

    Distinct from a red verdict: this means the repository declares
    push-triggered CI but the pushed candidate produced no runs, so there is
    nothing to gate on and proceeding would publish an unverified commit.
    """


def _workflow_triggers_on_push(path):
    """Return True if a workflow file declares a ``push`` trigger.

    Parsed rather than grepped so a ``push`` mentioned in a job step or a
    comment is not mistaken for a trigger. Unparseable files are treated as
    NOT push-triggered -- an unreadable workflow cannot be evidence that CI
    is expected.
    """
    from ruamel.yaml import YAML

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = YAML(typ="safe").load(f)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    # YAML 1.1 loaders turn the bare key ``on`` into the boolean True; YAML 1.2
    # keeps it a string. Accept both spellings.
    triggers = data.get("on", data.get(True))
    if isinstance(triggers, str):
        return triggers == "push"
    if isinstance(triggers, (list, tuple)):
        return "push" in triggers
    if isinstance(triggers, dict):
        return "push" in triggers
    return False


def push_triggered_workflows(repo_root=None):
    """Return the names of ``.github/workflows`` files that trigger on push.

    An empty result means the repository has no push-triggered CI: the release
    flow then proceeds without a CI gate instead of blocking forever on runs
    that can never appear. The distinction is an observable fact about the
    repository, not a fallback.
    """
    root = repo_root or _repo_root()
    workflow_dir = os.path.join(str(root), ".github", "workflows")
    if not os.path.isdir(workflow_dir):
        return []
    found = []
    for filename in sorted(os.listdir(workflow_dir)):
        if not filename.endswith((".yml", ".yaml")):
            continue
        if _workflow_triggers_on_push(os.path.join(workflow_dir, filename)):
            found.append(filename)
    return found


def wait_for_ci_green(commit_sha, *, timeout, check_filters, log=None,
                      config=None, repo_root=None, label=None,
                      discovery_grace=CI_DISCOVERY_GRACE_SECONDS):
    """Block until every CI run for *commit_sha* concludes.

    Returns ``(verdict, results)`` where verdict is one of :data:`CI_GREEN`,
    :data:`CI_RED`, :data:`CI_TIMEOUT`, or :data:`CI_NOT_CONFIGURED`.

    Five explicit outcomes, no silent waits:

    - The repository declares no push-triggered workflow -> :data:`CI_NOT_CONFIGURED`
      immediately (nothing can ever run; blocking would hang the release).
    - Push-triggered workflows exist but no run appears for the commit within
      the discovery grace -> :class:`CIWaitError` (hard error).
    - Runs appear and conclude -> :data:`CI_GREEN` / :data:`CI_RED` (transient
      failures retried once, deterministic ones not).
    - Runs appear but *timeout* expires with some still unresolved ->
      :data:`CI_TIMEOUT`. Distinct from red on purpose: nothing was proven
      about those runs, so the remedy is to check their status, not to fix
      code that may be perfectly fine.
    - Every run concludes green, but the RELEASING PROJECT'S OWN check runs
      were absent or did not conclude ``success`` (typically ``skipped`` by
      the monorepo CI router's paths filter) ->
      :class:`rlsbl.ci_checks.ProjectCINotRunError` (hard error). A green
      workflow run is not evidence that this project's CI ran: this is the
      exact predicate the publish gate applies later, checked here so the two
      gates cannot disagree and tag a version that can never publish.

    *check_filters* is mandatory (a list of
    :class:`rlsbl.ci_checks.CheckFilter`, from
    :func:`rlsbl.ci_checks.release_check_filters`) precisely because it must
    never be forgotten: a caller that omitted it would silently re-open the
    divergence. Pass an empty list only when there is no project to verify.

    *timeout* is the WHOLE budget: discovery is spent inside it and is capped
    at half of it, so the completion wait always keeps at least half.
    """
    def _log(msg):
        if log is not None:
            log(msg)
        else:
            print(msg, file=sys.stderr)

    expected = push_triggered_workflows(repo_root)
    if not expected:
        return CI_NOT_CONFIGURED, []

    label = label or f"candidate {commit_sha[:12]}"

    try:
        repo_slug = json.loads(
            run_gh(["repo", "view", "--json", "nameWithOwner"], config=config)
        ).get("nameWithOwner", "")
    except Exception:
        repo_slug = ""

    effective_grace, clamped = _discovery_budget(timeout, discovery_grace)
    attempts = max(1, int(effective_grace // max(1, CI_DISCOVERY_INTERVAL)))
    _log(
        f"Waiting for CI on the release candidate {commit_sha[:12]} "
        f"({len(expected)} push-triggered workflow file(s): {', '.join(expected)})..."
    )
    if clamped:
        _log(
            f"Run-discovery grace clamped to {effective_grace}s (normally "
            f"{discovery_grace}s): it is spent inside the {timeout}s CI budget, "
            f"and at least half of that budget stays reserved for the runs to "
            f"complete."
        )
    started = time.time()
    runs = poll_runs(commit_sha, max_attempts=attempts, interval=CI_DISCOVERY_INTERVAL)
    if not runs:
        raise CIWaitError(
            f"no CI runs appeared for the release candidate {commit_sha} within "
            f"{effective_grace}s, but this repository declares push-triggered "
            f"workflow(s): {', '.join(expected)}.\n"
            f"The candidate is on the remote and nothing was tagged or "
            f"published. Investigate why the push produced no runs (branch or "
            f"paths filters, disabled workflows, Actions quota), then re-run "
            f"`rlsbl release resume`."
        )

    _log(f"Found {len(runs)} CI run(s) for {commit_sha[:12]}; waiting for completion...")

    retried_lock = threading.Lock()
    retried_workflows = set()
    known_ids = {str(r["databaseId"]) for r in runs}

    def _remaining():
        return int(timeout - (time.time() - started))

    remaining = _remaining()
    if remaining < CI_MIN_COMPLETION_WINDOW:
        # Only reachable with a budget too small to hold a real wait; starting
        # a sub-floor wait would report a "timeout" that measured nothing.
        _log(
            f"CI budget of {timeout}s is spent (only {remaining}s left, floor "
            f"is {CI_MIN_COMPLETION_WINDOW}s); not starting a completion wait."
        )
        return CI_TIMEOUT, [
            {"name": r.get("name", f"run {r['databaseId']}"), "passed": False,
             "timed_out": True, "run_id": str(r["databaseId"])}
            for r in runs
        ]

    results = _watch_runs(runs, label, repo_slug, retried_lock, retried_workflows,
                          timeout=remaining)

    # Late-starting runs (a matrix leg or a second workflow created after the
    # first poll) must not escape the gate.
    time.sleep(5)
    late_runs = [
        r for r in poll_runs(commit_sha, max_attempts=1, interval=0)
        if str(r["databaseId"]) not in known_ids
    ]
    if late_runs:
        remaining = _remaining()
        if remaining < CI_MIN_COMPLETION_WINDOW:
            _log(
                f"Found {len(late_runs)} late-starting CI run(s), but the "
                f"{timeout}s CI budget is spent ({remaining}s left, floor is "
                f"{CI_MIN_COMPLETION_WINDOW}s); they stay unresolved."
            )
            results.extend(
                {"name": r.get("name", f"run {r['databaseId']}"), "passed": False,
                 "timed_out": True, "run_id": str(r["databaseId"])}
                for r in late_runs
            )
        else:
            _log(f"Found {len(late_runs)} late-starting CI run(s); waiting...")
            results.extend(
                _watch_runs(late_runs, label, repo_slug, retried_lock,
                            retried_workflows, timeout=remaining)
            )

    verdict = _timeout_verdict(results)

    # The workflow runs are green -- but a run whose only job for THIS project
    # was skipped is green too. Confirm the project's own check runs with the
    # publish gate's own predicate before the caller is allowed to tag.
    if verdict == CI_GREEN:
        verify_project_ci_ran(
            commit_sha, check_filters, cwd=repo_root, config=config, log=_log,
        )

    return verdict, results


def run_cmd(registry, args, flags):
    """Watch all CI runs for a commit until they complete.

    Usage: rlsbl watch [<commit-sha>]
           rlsbl watch --run-id <id> [--run-id <id2>]
    Defaults to HEAD if no commit SHA is provided.
    """
    run_ids = flags.get("run-id", [])
    if run_ids:
        try:
            runs = _resolve_run_ids(run_ids)
            if not runs:
                print("Error: no valid run IDs provided", file=sys.stderr)
                sys.exit(1)

            # Get repo info for display
            try:
                repo_info = json.loads(run_gh(["repo", "view", "--json", "nameWithOwner,name"]))
                repo_slug = repo_info.get("nameWithOwner", "")
            except Exception:
                print("Error: could not get repo info. Is gh installed and authenticated?", file=sys.stderr)
                sys.exit(1)

            label = f"run IDs {','.join(str(r) for r in run_ids)}"

            print(f"rlsbl: {label}: watching {len(runs)} run(s)...", file=sys.stderr)
            results = _watch_runs(runs, label, repo_slug)
            _print_workflow_audit(results)

            # Desktop notification with aggregated results
            passed = sum(1 for r in results if r["passed"])
            failed = len(results) - passed
            if failed:
                body = f"{passed}/{len(results)} passed, {failed} failed"
                failed_run = next((r for r in results if not r["passed"]), None)
                fail_url = None
                if failed_run and repo_slug and failed_run.get("run_id"):
                    fail_url = f"https://github.com/{repo_slug}/actions/runs/{failed_run['run_id']}"
                _notify(f"{label}: CI FAILED", body, url=fail_url)
            else:
                body = f"{len(results)}/{len(results)} passed"
                success_url = _release_url(repo_slug)
                _notify(f"{label}: CI passed", body, url=success_url)

            sys.exit(1 if failed else 0)
        except KeyboardInterrupt:
            print("\nWatch cancelled.", file=sys.stderr)
            sys.exit(130)

    try:
        # Get commit SHA (resolve short SHAs -- gh requires full 40-char)
        if args:
            try:
                commit_sha = run("git", ["rev-parse", args[0]])
            except Exception:
                commit_sha = args[0]
        else:
            try:
                commit_sha = run("git", ["rev-parse", "HEAD"])
            except Exception:
                print("Error: not a git repository and no commit SHA provided.", file=sys.stderr)
                sys.exit(1)

        # Get repo info for display and URLs
        try:
            repo_info = run_gh(["repo", "view", "--json", "nameWithOwner,name"])
            info = json.loads(repo_info)
            repo_slug = info.get("nameWithOwner", "")
            repo_name = info.get("name", "")
        except Exception:
            print("Error: could not get repo info. Is gh installed and authenticated?", file=sys.stderr)
            sys.exit(1)

        # Try to find a tag for this commit for nicer display
        try:
            tag = run("git", ["describe", "--tags", "--exact-match", commit_sha])
        except Exception:
            tag = commit_sha[:12]

        label = f"{repo_name} {tag}" if repo_name else tag

        # Poll until at least one run appears (poll budget ~120s:
        # 30 attempts x 4s interval, see poll_runs defaults)
        runs = poll_runs(commit_sha)

        if not runs:
            print(
                f"No CI runs found for {commit_sha[:12]}. GitHub Actions may not have "
                f"triggered yet. Run `rlsbl watch {commit_sha[:12]}` to check later.",
                file=sys.stderr,
            )
            # Best-effort hint: if this commit has a GitHub Release but no
            # workflows ran, suggest `rlsbl release retry`.
            try:
                release_tag = run("git", ["describe", "--tags", "--exact-match", commit_sha])
                try:
                    run_gh(["release", "view", release_tag])
                    print(
                        f"rlsbl: hint: GitHub Release {release_tag} exists but "
                        "no workflows ran. Try: rlsbl release retry",
                        file=sys.stderr,
                    )
                except Exception:
                    pass
            except Exception:
                pass
            sys.exit(0)

        print(f"rlsbl: {label}: found {len(runs)} CI run(s), watching...", file=sys.stderr)

        # Shared retry-dedup state, carried across the initial watch and the
        # late re-poll watch so retries are never dispatched twice for the
        # same workflow.
        retried_lock = threading.Lock()
        retried_workflows = set()
        # Run IDs seen in the initial set, used to distinguish genuinely
        # late-starting runs from already-watched ones. Retries are in-place
        # reruns that keep their original id, so a reran run is already in
        # this set and is never mistaken for a late-starting run.
        known_ids = {str(r["databaseId"]) for r in runs}

        # Watch runs in parallel
        results = _watch_runs(runs, label, repo_slug, retried_lock, retried_workflows)

        # Re-poll for late-starting workflows (e.g. Publish triggered by
        # a GitHub Release that was created after CI started).  Wait briefly,
        # then check once for any runs that were not in the initial set.
        time.sleep(5)
        all_runs_now = poll_runs(commit_sha, max_attempts=1, interval=0)
        late_runs = [r for r in all_runs_now if str(r["databaseId"]) not in known_ids]

        if late_runs:
            print(
                f"rlsbl: {label}: found {len(late_runs)} late-starting run(s), watching...",
                file=sys.stderr,
            )
            late_results = _watch_runs(late_runs, label, repo_slug, retried_lock,
                                       retried_workflows)
            results.extend(late_results)

        # Workflow audit: list what ran and flag missing publish workflows
        # (runs after re-poll so it sees all workflows including late ones)
        _print_workflow_audit(results)

        # Desktop notification with aggregated results
        passed = sum(1 for r in results if r["passed"])
        failed = len(results) - passed
        if failed:
            body = f"{passed}/{len(results)} passed, {failed} failed"
            failed_run = next((r for r in results if not r["passed"]), None)
            fail_url = None
            if failed_run and repo_slug and failed_run.get("run_id"):
                fail_url = f"https://github.com/{repo_slug}/actions/runs/{failed_run['run_id']}"
            _notify(f"{label}: CI FAILED", body, url=fail_url)
        else:
            body = f"{len(results)}/{len(results)} passed"
            success_url = None
            if repo_slug and tag and not tag.startswith(commit_sha[:8]):
                success_url = f"https://github.com/{repo_slug}/releases/tag/{tag}"
            else:
                success_url = _release_url(repo_slug)
            _notify(f"{label}: CI passed", body, url=success_url)

        sys.exit(1 if failed else 0)
    except KeyboardInterrupt:
        print("\nWatch cancelled.", file=sys.stderr)
        sys.exit(130)
