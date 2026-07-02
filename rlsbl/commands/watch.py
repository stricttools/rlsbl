"""Watch command that polls GitHub Actions CI workflow runs for a given commit SHA and reports pass, fail, or in-progress status."""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..utils import require_tool, run, run_gh


def _open_url(url):
    """Open a URL in the default browser. Non-fatal if unavailable."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], timeout=5, capture_output=True)
        else:
            subprocess.run(["xdg-open", url], timeout=5, capture_output=True)
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
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{escaped_body}" with title "{escaped_title}"'],
                timeout=5, capture_output=True,
            )
        elif require_tool("notify-send", fatal=False):
            cmd = ["notify-send", "-u", "normal"]
            if url:
                cmd += ["--action", "open=Open"]
            cmd += [title, body]
            result = subprocess.run(cmd, timeout=120, capture_output=True, text=True)
            if url and result.stdout.strip() == "open":
                _open_url(url)
    except Exception:
        pass


def _retry_workflow(workflow_name, branch, repo_slug, label):
    """Re-trigger a workflow once and watch the retry run.

    Returns a result dict with name, passed, and run_id.
    Returns None if the retry could not be triggered or found.
    """
    print(f"rlsbl: {label}: [{workflow_name}] CI failed, retrying once...", file=sys.stderr)
    try:
        run_gh(["workflow", "run", workflow_name, "--ref", branch], timeout=30)
    except Exception as exc:
        print(f"rlsbl: {label}: [{workflow_name}] retry trigger failed: {exc}", file=sys.stderr)
        return None

    # Poll for the new run to appear (up to 30s)
    retry_run = None
    for _ in range(15):
        time.sleep(2)
        try:
            raw = run_gh(["run", "list",
                         f"--workflow={workflow_name}",
                         f"--branch={branch}",
                         "--json", "databaseId,name,status,createdAt",
                         "--limit", "1"])
            parsed = json.loads(raw)
            if parsed:
                retry_run = parsed[0]
                break
        except Exception:
            pass

    if not retry_run:
        print(f"rlsbl: {label}: [{workflow_name}] retry run not found after 30s", file=sys.stderr)
        return None

    retry_id = str(retry_run["databaseId"])
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
        return {"name": workflow_name, "passed": False, "run_id": retry_id}
    except subprocess.TimeoutExpired:
        print(f"rlsbl: {label}: [{workflow_name}] retry timed out after 1h", file=sys.stderr)
        return {"name": workflow_name, "passed": False, "run_id": retry_id}
    except Exception as exc:
        print(f"rlsbl: {label}: [{workflow_name}] retry error: {exc}", file=sys.stderr)
        return {"name": workflow_name, "passed": False, "run_id": retry_id}


def _watch_single_run(ci_run, label, repo_slug, retried_lock=None, retried_workflows=None,
                      known_ids=None):
    """Watch a single CI run. Returns a dict with name, passed, and run_id.

    When retried_lock and retried_workflows are provided, deduplicates retries
    so that only one retry is dispatched per workflow name even when multiple
    runs from the same workflow fail concurrently.

    When known_ids (a shared set of run-id strings) is provided, run IDs of
    dispatched retry runs are recorded in it so the caller can recognize them
    later (e.g. the late re-poll must not treat a retry run as a new run).
    """
    run_id = str(ci_run["databaseId"])
    workflow_name = ci_run.get("name", f"run {run_id}")

    try:
        # gh run watch blocks until the run completes;
        # --exit-status makes it exit 1 on failure; check=True raises
        # CalledProcessError so we can distinguish pass from fail
        run_gh(["run", "watch", run_id, "--exit-status"], timeout=3600)
        msg = f"rlsbl: {label}: [{workflow_name}] passed"
        print(msg, file=sys.stderr)
        return {"name": workflow_name, "passed": True, "run_id": run_id}
    except subprocess.CalledProcessError:
        msg = f"rlsbl: {label}: [{workflow_name}] FAILED"
        print(msg, file=sys.stderr)
        if repo_slug:
            print(f"rlsbl: https://github.com/{repo_slug}/actions/runs/{run_id}",
                  file=sys.stderr)
        # Auto-retry once before reporting failure (deduplicated by workflow name)
        branch = ci_run.get("headBranch")
        if branch and workflow_name:
            should_retry = True
            if retried_lock is not None and retried_workflows is not None:
                with retried_lock:
                    if workflow_name in retried_workflows:
                        should_retry = False
                    else:
                        retried_workflows.add(workflow_name)
            if should_retry:
                retry_result = _retry_workflow(workflow_name, branch, repo_slug, label)
                if retry_result is not None:
                    if known_ids is not None and retry_result.get("run_id"):
                        known_ids.add(str(retry_result["run_id"]))
                    return retry_result
        return {"name": workflow_name, "passed": False, "run_id": run_id}
    except subprocess.TimeoutExpired:
        msg = f"rlsbl: {label}: [{workflow_name}] timed out after 1h"
        print(msg, file=sys.stderr)
        return {"name": workflow_name, "passed": False, "run_id": run_id}
    except Exception as exc:
        msg = f"rlsbl: {label}: [{workflow_name}] error: {exc}"
        print(msg, file=sys.stderr)
        return {"name": workflow_name, "passed": False, "run_id": run_id}


def _watch_runs(runs, label, repo_slug, retried_lock=None, retried_workflows=None,
                known_ids=None):
    """Watch all runs in parallel. Returns list of result dicts.

    retried_lock, retried_workflows, and known_ids may be passed in so that
    retry deduplication state is shared across multiple _watch_runs calls
    (initial watch + late re-poll watch). Fresh state is created when omitted.

    Single-run pools deliberately go through the same thread-pool path so
    every run participates in the shared retry-dedup machinery.
    """
    if retried_lock is None:
        retried_lock = threading.Lock()
    if retried_workflows is None:
        retried_workflows = set()
    if known_ids is None:
        known_ids = set()
    known_ids.update(str(r["databaseId"]) for r in runs)

    results = []
    with ThreadPoolExecutor(max_workers=len(runs)) as executor:
        futures = {
            executor.submit(_watch_single_run, ci_run, label, repo_slug, retried_lock,
                            retried_workflows, known_ids): ci_run
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


def _has_publish_workflow_on_disk():
    """Check if any .github/workflows file looks like a publish workflow."""
    workflow_dir = os.path.join(".github", "workflows")
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

        # Poll until at least one run appears (retry up to 30s)
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
        # same workflow, and retry run IDs are recognized in the re-poll.
        retried_lock = threading.Lock()
        retried_workflows = set()
        known_ids = {str(r["databaseId"]) for r in runs}

        # Watch runs in parallel
        results = _watch_runs(runs, label, repo_slug, retried_lock, retried_workflows,
                              known_ids)

        # Re-poll for late-starting workflows (e.g. Publish triggered by
        # a GitHub Release that was created after CI started).  Wait briefly,
        # then check once for any runs that were not in the initial set.
        # known_ids also contains run IDs of retries dispatched during the
        # initial watch -- those run on the same commit SHA and must not be
        # mistaken for late-starting runs.
        time.sleep(5)
        all_runs_now = poll_runs(commit_sha, max_attempts=1, interval=0)
        late_runs = [r for r in all_runs_now if str(r["databaseId"]) not in known_ids]

        if late_runs:
            print(
                f"rlsbl: {label}: found {len(late_runs)} late-starting run(s), watching...",
                file=sys.stderr,
            )
            late_results = _watch_runs(late_runs, label, repo_slug, retried_lock,
                                       retried_workflows, known_ids)
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
