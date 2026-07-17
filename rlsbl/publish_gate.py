"""Single source of truth for the publish CI gate, generating the GitHub Actions gate job that blocks publishing until all CI checks pass on the release commit.

Publish workflows trigger on ``release: published`` and ``workflow_dispatch``
and would otherwise RACE CI on the same commit -- a broken artifact could
publish before CI reported. Every publish workflow therefore starts with a
``gate`` job, defined once here and injected everywhere:

- standalone templates get it via the ``{{publishGate}}`` template variable
  (rendered by :func:`gate_job_template_snippet`);
- the merged multi-target publish workflow embeds :func:`build_gate_job`;
- the monorepo publish router embeds :func:`build_router_gate_job`.

The gate resolves the release commit MARKER-FIRST: it reads the exact commit
CI ran on from the ``<!-- rlsbl-ci-sha: <40-hex> -->`` marker rlsbl writes
into the GitHub Release body, and falls back to ``$GITHUB_SHA`` (the tag's
commit for both ``release: published`` and ``workflow_dispatch`` at the tag
ref) for older releases that predate the marker. It never reads the release
event payload (dispatch retries have none). It then polls the GitHub checks
API until the releasing project's CI check runs complete, collapsing retried
same-named check-runs to the latest so a superseded failure cannot block.

Conclusion semantics (explicit, no silent waits):

- ``success`` on every matching check -> the gate passes.
- ``failure`` / ``timed_out`` -> hard error (CI did not pass).
- ``cancelled`` / ``skipped`` -> hard error with an explanation; a
  cancelled or skipped check proves nothing about the commit, so the gate
  never treats it as passing and never waits forever for it.
- No matching check runs after a grace window -> hard error; a scaffolded
  repository always has CI, so the release commit must produce check runs.
"""

from __future__ import annotations

import re
from io import StringIO

from ruamel.yaml import YAML

GATE_JOB_KEY = "gate"

# Tunables surfaced as job env so users can adjust them by editing the
# generated workflow (configurable-by-edit; no flags, no payload inputs).
GATE_TIMEOUT_MINUTES = "20"
GATE_GRACE_MINUTES = "5"
GATE_POLL_SECONDS = "15"

# One publish pipeline per tag: a dispatch retry at the same tag queues
# behind an in-flight run instead of racing it. Publishes are never
# cancelled mid-flight (cancel-in-progress: false). Uses inputs.tag
# (from workflow_dispatch) with fallback to github.ref_name so both
# release events and manual dispatch converge on the same group key.
PUBLISH_CONCURRENCY_GROUP = "publish-${{ inputs.tag || github.ref_name }}"


def publish_concurrency_block() -> dict:
    """Workflow-level concurrency mapping for publish workflows."""
    return {
        "group": PUBLISH_CONCURRENCY_GROUP,
        "cancel-in-progress": False,
    }


# CI job names per target, as scaffolded by each target's ci*.yml.tpl.
# Check runs on a commit are named after these jobs (matrix jobs append
# " (...)"), which is what the gate's name filter matches against.
CI_CHECK_JOB_NAMES = {
    "cargo": ("test",),
    "deno": ("test",),
    "docker": ("build",),
    "go": ("test",),
    "hex": ("test",),
    "maven": ("test",),
    "npm": ("test",),
    "pypi": ("test",),
    "spec": ("validate",),
    "swift": ("test",),
    "swift-apple": ("test",),
    "zig": ("test",),
}
_DEFAULT_CI_CHECK_JOB_NAMES = ("test",)


def ci_check_regex_for_targets(targets: list[str]) -> str:
    """Build the check-run name regex for a set of scaffolded targets.

    The regex matches each target's CI job names exactly, plus their matrix
    expansions (``test (20)``). Targets without a known CI template fall
    back to the conventional ``test`` job name.
    """
    names: set[str] = set()
    for target in targets:
        names.update(CI_CHECK_JOB_NAMES.get(target, _DEFAULT_CI_CHECK_JOB_NAMES))
    if not names:
        names.update(_DEFAULT_CI_CHECK_JOB_NAMES)
    alternation = "|".join(re.escape(n) for n in sorted(names))
    return rf"^({alternation})( \(.*\))?$"


# The poll loop. Reads only environment provided by the runner and the gate
# job env (CI_CHECK_REGEX, GATE_*): no GitHub expressions, no event payload.
GATE_POLL_SCRIPT = """\
set -euo pipefail

# Commit resolution -- explicit two-step order (never reads the release
# event payload; dispatch retries have none):
#   (1) Marker: rlsbl writes the exact commit CI ran on into the GitHub
#       Release body as a machine-parseable line of the form
#           <!-- rlsbl-ci-sha: <40-hex> -->
#       Prefer it when present -- it pins the precise commit and is immune to
#       ref races. The tag is inputs.tag (TAG_INPUT) when dispatched with an
#       explicit override, else GITHUB_REF_NAME (matches the router resolver).
#   (2) Fallback: older releases predate the marker, so fall back to
#       $GITHUB_SHA, the tag's commit for both release and dispatch-at-tag.
tag="${TAG_INPUT:-$GITHUB_REF_NAME}"
sha=""
if body="$(gh release view "$tag" --json body --jq .body 2>/dev/null)"; then
  sha="$(printf '%s\\n' "$body" \\
    | sed -n 's/.*<!-- rlsbl-ci-sha: \\([0-9a-f]\\{40\\}\\) -->.*/\\1/p' \\
    | head -n1)"
fi
if [ -n "$sha" ]; then
  echo "Publish gate: resolved release commit from rlsbl-ci-sha marker in the '$tag' release body."
else
  sha="$GITHUB_SHA"
  echo "Publish gate: no rlsbl-ci-sha marker in the '$tag' release body; falling back to \\$GITHUB_SHA."
fi
echo "Publish gate: waiting for CI on $GITHUB_REF_NAME (commit $sha)"
echo "Check-run name filter: $CI_CHECK_REGEX"
# Timeout, grace window, and poll interval come from the job env above;
# edit them there if this repository's CI needs different limits.

now() { date +%s; }
start="$(now)"
deadline=$(( start + GATE_TIMEOUT_MINUTES * 60 ))
grace_deadline=$(( start + GATE_GRACE_MINUTES * 60 ))

while :; do
  if ! resp="$(gh api --paginate "repos/$GITHUB_REPOSITORY/commits/$sha/check-runs?per_page=100")"; then
    echo "Checks API request failed; retrying in ${GATE_POLL_SECONDS}s..."
    sleep "$GATE_POLL_SECONDS"
    continue
  fi
  # Match this project's CI check runs by name; exclude check runs that
  # belong to THIS workflow run (the gate itself and the queued publish
  # jobs would otherwise deadlock the poll loop).
  # A retried CI run creates a BRAND-NEW check-run with the same name as the
  # old one, so a stale failure would block the gate forever. Project id and
  # started_at, then reduce to the latest check-run per name (max started_at,
  # numeric id as tiebreak for retries within the same second) BEFORE the
  # pending / not-success logic runs. This way the newest run decides: still
  # running -> wait; genuinely red -> hard fail.
  runs="$(jq -s --arg re "$CI_CHECK_REGEX" --arg run_id "$GITHUB_RUN_ID" '
    [ .[].check_runs[]
      | select(.name | test($re))
      | select((.details_url // "") | contains("/actions/runs/" + $run_id + "/") | not)
      | {name, status, conclusion, id, started_at} ]
    | group_by(.name)
    | map(sort_by(.started_at, .id) | last)' <<< "$resp")"
  total="$(jq 'length' <<< "$runs")"

  if [ "$total" -eq 0 ]; then
    if [ "$(now)" -ge "$grace_deadline" ]; then
      echo "::error::Publish gate: no CI check runs matching $CI_CHECK_REGEX appeared on $sha within $GATE_GRACE_MINUTES minutes."
      echo "A scaffolded repository always has a CI workflow, so the release commit must produce CI check runs."
      echo "If CI jobs were renamed, update CI_CHECK_REGEX in this workflow's gate job to match the new names."
      exit 1
    fi
    echo "No matching CI check runs yet; retrying in ${GATE_POLL_SECONDS}s..."
    sleep "$GATE_POLL_SECONDS"
    continue
  fi

  pending="$(jq '[ .[] | select(.status != "completed") ] | length' <<< "$runs")"
  if [ "$pending" -gt 0 ]; then
    if [ "$(now)" -ge "$deadline" ]; then
      echo "::error::Publish gate: timed out after $GATE_TIMEOUT_MINUTES minutes waiting for CI to complete on $sha."
      jq -r '.[] | "  \\(.name): status=\\(.status) conclusion=\\(.conclusion // "none")"' <<< "$runs"
      exit 1
    fi
    echo "$pending of $total matching CI check runs still running; retrying in ${GATE_POLL_SECONDS}s..."
    sleep "$GATE_POLL_SECONDS"
    continue
  fi

  not_success="$(jq '[ .[] | select(.conclusion != "success") ]' <<< "$runs")"
  if [ "$(jq 'length' <<< "$not_success")" -gt 0 ]; then
    echo "::error::Publish gate: CI did not pass on $sha -- refusing to publish."
    jq -r '.[] | "  \\(.name): \\(.conclusion)"' <<< "$not_success"
    while IFS= read -r conclusion; do
      case "$conclusion" in
        failure|timed_out)
          echo "CI concluded '$conclusion' on the release commit. Fix the failure, re-run the CI workflow to green on this exact commit (gh run rerun <run-id>), then re-dispatch this publish workflow at the tag ref: gh workflow run <publish workflow> --ref $GITHUB_REF_NAME"
          ;;
        cancelled)
          echo "A CI check run was CANCELLED. A cancelled run proves nothing about the commit, so the gate treats it as a hard failure instead of waiting for a conclusion that will never come. Re-run the cancelled CI workflow (gh run rerun <run-id>), then re-dispatch this publish workflow at the tag ref."
          ;;
        skipped)
          echo "A CI check run matching the filter was SKIPPED. The gate cannot treat a skipped check as passing: this project's own CI must actually run on the release commit. Check paths filters and job conditions, re-run CI on this commit, then re-dispatch this publish workflow at the tag ref."
          ;;
        *)
          echo "CI check concluded '$conclusion' (not success). The gate only proceeds when every matching check concluded success."
          ;;
      esac
    done <<< "$(jq -r '.[].conclusion' <<< "$not_success" | sort -u)"
    exit 1
  fi

  echo "Publish gate: all $total matching CI check runs succeeded."
  jq -r '.[] | "  \\(.name): \\(.conclusion)"' <<< "$runs"
  exit 0
done
"""


def build_gate_job(check_regex: str | None = None, resolver_script: str | None = None) -> dict:
    """Build the gate job as a workflow-jobs dict entry.

    ``check_regex`` bakes the check-run name filter into the job env
    (standalone and merged workflows, where the releasing project is the
    repository itself). ``resolver_script`` prepends a step that resolves
    ``CI_CHECK_REGEX`` at runtime (the monorepo router, where the releasing
    project is derived from the tag ref).
    """
    env = {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "GATE_TIMEOUT_MINUTES": GATE_TIMEOUT_MINUTES,
        "GATE_GRACE_MINUTES": GATE_GRACE_MINUTES,
        "GATE_POLL_SECONDS": GATE_POLL_SECONDS,
    }
    if check_regex is not None:
        env["CI_CHECK_REGEX"] = check_regex

    steps = []
    if resolver_script is not None:
        steps.append(
            {
                "name": "Resolve releasing project's CI checks from the tag ref",
                "run": resolver_script,
            }
        )
    steps.append(
        {
            "name": "Wait for CI to succeed on the release commit",
            "run": GATE_POLL_SCRIPT,
        }
    )

    return {
        "name": "Gate on CI",
        "runs-on": "ubuntu-latest",
        # Job-level permissions: exactly what the gate needs, regardless of
        # (restrictive) workflow-level permission blocks around it.
        "permissions": {"checks": "read"},
        "env": env,
        "steps": steps,
    }


def build_router_gate_job(prefix_regex_pairs: list[tuple[str, str]]) -> dict:
    """Build the shared gate for the monorepo publish router.

    ``prefix_regex_pairs`` maps each project's tag prefix to the regex
    matching its prefixed CI check-run names (the CI router invokes member
    CI as reusable workflows, so check runs are named
    ``<router job key> / <ci job name>``). Longer prefixes are matched
    first so overlapping project names resolve to the right project.
    """
    lines = [
        "set -euo pipefail",
        "",
        "# rlsbl retry contract: to retry a release publish, dispatch this",
        "# workflow AT THE TAG REF (gh workflow run publish.yml --ref <tag>).",
        "# The tag prefix selects the releasing project; a bare dispatch from",
        "# a branch matches no project and fails here instead of silently",
        "# skipping every job.",
        "#",
        "# inputs.tag (TAG_INPUT) overrides GITHUB_REF_NAME so web-UI dispatch",
        "# with an explicit tag input also resolves the correct project.",
        'tag_ref="${TAG_INPUT:-$GITHUB_REF_NAME}"',
        'case "$tag_ref" in',
    ]
    for prefix, regex in sorted(
        prefix_regex_pairs, key=lambda pair: len(pair[0]), reverse=True
    ):
        lines.append(f'  "{prefix}"*)')
        lines.append(f"    regex='{regex}'")
        lines.append("    ;;")
    known = ", ".join(prefix for prefix, _ in prefix_regex_pairs)
    lines.extend(
        [
            "  *)",
            f'    echo "::error::Publish gate: ref \'$tag_ref\' matches no project tag prefix (known prefixes: {known})."',
            '    echo "To retry a release publish, dispatch this workflow at the tag ref: gh workflow run publish.yml --ref <tag>"',
            "    exit 1",
            "    ;;",
            "esac",
            'echo "CI_CHECK_REGEX=$regex" >> "$GITHUB_ENV"',
            'echo "Releasing project CI check filter: $regex"',
        ]
    )
    resolver_script = "\n".join(lines) + "\n"
    job = build_gate_job(resolver_script=resolver_script)
    # Pass the workflow_dispatch tag input so the resolver can fall back to it
    # when dispatched from the web UI with an explicit tag override.
    job["env"]["TAG_INPUT"] = "${{ inputs.tag }}"
    return job


def _literal_str_representer(representer, data):
    """Represent multi-line strings with ``|`` literal block style."""
    if "\n" in data:
        return representer.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return representer.represent_scalar("tag:yaml.org,2002:str", data)


def gate_job_template_snippet(check_regex: str) -> str:
    """Render the gate job as a YAML snippet for the ``{{publishGate}}`` var.

    Returns the ``gate:`` job block indented two spaces so it slots directly
    under the ``jobs:`` key of a publish template. No trailing newline (the
    placeholder occupies its own line in the template).
    """
    yml = YAML()
    yml.default_flow_style = False
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.width = 4096
    yml.representer.add_representer(str, _literal_str_representer)
    stream = StringIO()
    yml.dump({GATE_JOB_KEY: build_gate_job(check_regex=check_regex)}, stream)
    text = stream.getvalue().rstrip("\n")
    return "\n".join(
        ("  " + line) if line.strip() else line for line in text.splitlines()
    )
