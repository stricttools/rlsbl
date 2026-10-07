  wait-for-ci:
    name: Wait for CI
    runs-on: ubuntu-latest
    timeout-minutes: {{jobTimeoutMinutes}}
    # What this job needs, whatever the workflow grants: checks:read polls
    # the release commit's check runs, and contents:read reads the
    # rlsbl-ci-sha marker from the Release body.
    permissions:
      checks: read
      contents: read
    # Edit these to change how long this job waits; raising
    # WAIT_TIMEOUT_MINUTES needs timeout-minutes above raised with it.
    env:
      GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      # The job has no checkout, so gh needs the repository named to read the
      # Release body.
      GH_REPO: ${{ github.repository }}
      WAIT_TIMEOUT_MINUTES: "{{timeoutMinutes}}"
      WAIT_GRACE_MINUTES: "{{graceMinutes}}"
      WAIT_POLL_SECONDS: "{{pollSeconds}}"
      WAIT_MARKER_ATTEMPTS: "{{markerAttempts}}"
      WAIT_MARKER_RETRY_SECONDS: "{{markerRetrySeconds}}"{{#if checkPattern}}
      CI_CHECK_REGEX: '{{checkPattern}}'{{/if}}{{#if tagInput}}
      TAG_INPUT: ${{ inputs.tag }}{{/if}}
    steps:{{#if resolverScript}}
      - name: Resolve the releasing project's CI checks from the tag
        run: |
{{resolverScript}}{{/if}}
      - name: Wait for CI to succeed on the release commit
        run: |
          set -euo pipefail

          # The release commit: rlsbl writes the commit CI ran on into the
          # GitHub Release body as a line of the form
          #     <!-- rlsbl-ci-sha: <40-hex> -->
          # and that marker is read first. A Release created moments ago can
          # lag on GitHub's API, so the read is retried before the marker is
          # taken to be absent; only then is $GITHUB_SHA (the tag's commit)
          # used, for a release older than the marker. The release event's
          # payload is never read: a dispatch retry has none.
          extract_ci_sha() {
            sed -n 's/.*<!-- rlsbl-ci-sha: \([0-9a-f]\{40\}\) -->.*/\1/p' | head -n1
          }
          tag="${TAG_INPUT:-$GITHUB_REF_NAME}"
          sha=""
          attempt=1
          while [ "$attempt" -le "$WAIT_MARKER_ATTEMPTS" ]; do
            if body="$(gh release view "$tag" --json body --jq .body 2>/dev/null)"; then
              sha="$(printf '%s\n' "$body" | extract_ci_sha)"
            fi
            if [ -n "$sha" ]; then
              break
            fi
            if [ "$attempt" -lt "$WAIT_MARKER_ATTEMPTS" ]; then
              echo "wait-for-ci: the rlsbl-ci-sha marker is not yet visible in the '$tag' release body (attempt $attempt/$WAIT_MARKER_ATTEMPTS); retrying in ${WAIT_MARKER_RETRY_SECONDS}s..."
              sleep "$WAIT_MARKER_RETRY_SECONDS"
            fi
            attempt=$(( attempt + 1 ))
          done
          if [ -n "$sha" ]; then
            echo "wait-for-ci: the release commit comes from the rlsbl-ci-sha marker in the '$tag' release body."
          else
            sha="$GITHUB_SHA"
            echo "wait-for-ci: no rlsbl-ci-sha marker after $WAIT_MARKER_ATTEMPTS attempt(s) on the '$tag' release body; using \$GITHUB_SHA."
          fi
          echo "wait-for-ci: waiting for CI on $GITHUB_REF_NAME (commit $sha)"
          echo "Check-run name filter: $CI_CHECK_REGEX"

          now() { date +%s; }
          start="$(now)"
          deadline=$(( start + WAIT_TIMEOUT_MINUTES * 60 ))
          grace_deadline=$(( start + WAIT_GRACE_MINUTES * 60 ))

          while :; do
            if ! resp="$(gh api --paginate "repos/$GITHUB_REPOSITORY/commits/$sha/check-runs?per_page=100")"; then
              # The deadline holds here too: a checks API that keeps failing
              # must not hold the job until GitHub's six-hour job limit.
              if [ "$(now)" -ge "$deadline" ]; then
                echo "::error::wait-for-ci: the checks API request for $sha kept failing for $WAIT_TIMEOUT_MINUTES minutes."
                echo "Without the check runs this job cannot tell whether CI passed, so it refuses to publish."
                echo "Check the API response above and this job's checks:read permission, then re-dispatch this publish workflow at the tag ref: gh workflow run <publish workflow> --ref $GITHUB_REF_NAME"
                exit 1
              fi
              echo "Checks API request failed; retrying in ${WAIT_POLL_SECONDS}s..."
              sleep "$WAIT_POLL_SECONDS"
              continue
            fi
            # Matching check runs, minus this workflow run's own jobs (which
            # would otherwise wait on themselves). A retried CI run creates a
            # new check run of the same name, so each name is reduced to its
            # latest run (started_at, then id). A latest run that was skipped
            # is replaced by the latest completed run of that name that was
            # not skipped, because a router dispatch leaves a skipped run of
            # the push-triggered suite beside the dispatched one. A skipped
            # run is then dropped when a completed, not skipped run of the
            # same job's matrix expansion exists ("test" beside "test (22)"),
            # since GitHub does not expand a matrix for a job it skipped.
            # internal/ci reads check runs the same way, and the two must
            # not diverge.
            runs="$(jq -s --arg re "$CI_CHECK_REGEX" --arg run_id "$GITHUB_RUN_ID" '
              [ .[].check_runs[]
                | select(.name | test($re))
                | select((.details_url // "") | contains("/actions/runs/" + $run_id + "/") | not)
                | {name, status, conclusion, id, started_at} ]
              | group_by(.name)
              | map(
                  . as $g
                  | ($g | sort_by(.started_at, .id) | last) as $newest
                  | (if $newest.conclusion == "skipped"
                     then ([ $g[] | select(.status == "completed" and .conclusion != "skipped") ]
                           | sort_by(.started_at, .id) | last // $newest)
                     else $newest end))
              | . as $latest
              | map(. as $c
                  | select(
                      $c.conclusion != "skipped"
                      or ([ $latest[]
                            | select((.name | startswith($c.name + " ("))
                                     and .status == "completed"
                                     and .conclusion != "skipped") ]
                          | length) == 0))' <<< "$resp")"
            total="$(jq 'length' <<< "$runs")"

            if [ "$total" -eq 0 ]; then
              if [ "$(now)" -ge "$grace_deadline" ]; then
                echo "::error::wait-for-ci: no CI check runs matching $CI_CHECK_REGEX appeared on $sha within $WAIT_GRACE_MINUTES minutes."
                echo "A scaffolded repository always has a CI workflow, and rlsbl verifies CI on this commit before tagging it, so check runs must exist here."
                echo "Their absence means the check runs were deleted, the commit was resolved wrongly, or this tag was created outside rlsbl."
                echo "If CI jobs were renamed, update CI_CHECK_REGEX in this job to match the new names."
                exit 1
              fi
              echo "No matching CI check runs yet; retrying in ${WAIT_POLL_SECONDS}s..."
              sleep "$WAIT_POLL_SECONDS"
              continue
            fi

            pending="$(jq '[ .[] | select(.status != "completed") ] | length' <<< "$runs")"
            if [ "$pending" -gt 0 ]; then
              if [ "$(now)" -ge "$deadline" ]; then
                echo "::error::wait-for-ci: timed out after $WAIT_TIMEOUT_MINUTES minutes waiting for CI to complete on $sha."
                jq -r '.[] | "  \(.name): status=\(.status) conclusion=\(.conclusion // "none")"' <<< "$runs"
                exit 1
              fi
              echo "$pending of $total matching CI check runs still running; retrying in ${WAIT_POLL_SECONDS}s..."
              sleep "$WAIT_POLL_SECONDS"
              continue
            fi

            not_success="$(jq '[ .[] | select(.conclusion != "success") ]' <<< "$runs")"
            if [ "$(jq 'length' <<< "$not_success")" -gt 0 ]; then
              echo "::error::wait-for-ci: CI did not pass on $sha; refusing to publish."
              jq -r '.[] | "  \(.name): \(.conclusion)"' <<< "$not_success"
              while IFS= read -r conclusion; do
                case "$conclusion" in
                  failure|timed_out)
                    echo "CI concluded '$conclusion' on the release commit."
                    echo "rlsbl tags and releases a commit only after its CI has gone green, so this means one of: CI was re-run on an already released commit and regressed, a required check was added after the release, or this tag and Release were created outside rlsbl."
                    echo "Re-dispatching this publish workflow gives the same answer: a failure in the code at this commit fails the same way every time."
                    echo "Fix forward on the release branch and cut a new release with 'rlsbl release run' (its own CI must go green before it is tagged), then mark this one with 'rlsbl release deprecate <version>'."
                    ;;
                  cancelled)
                    echo "A CI check run was CANCELLED. A cancelled run proves nothing about the commit, so it fails this job instead of being waited for."
                    echo "Re-run the cancelled CI workflow on this commit (gh run rerun <run-id>); if it concludes success, re-dispatch this publish workflow at the tag ref: gh workflow run <publish workflow> --ref $GITHUB_REF_NAME"
                    ;;
                  skipped)
                    echo "A CI check run matching the filter was SKIPPED, and a skipped check is not a passing one: this project must run its own CI on the release commit."
                    echo "Check the paths filters and job conditions so CI runs for this project, re-run CI on this commit, then re-dispatch this publish workflow at the tag ref."
                    ;;
                  *)
                    echo "CI check concluded '$conclusion' (not success). Publishing proceeds only when every matching check concluded success."
                    ;;
                esac
              done <<< "$(jq -r '.[].conclusion' <<< "$not_success" | sort -u)"
              exit 1
            fi

            echo "wait-for-ci: all $total matching CI check runs succeeded."
            jq -r '.[] | "  \(.name): \(.conclusion)"' <<< "$runs"
            exit 0
          done
