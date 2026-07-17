"""Executable tests for the publish gate poll script's bash/jq logic.

Unlike ``test_publish_gate.py`` (which checks the generated workflow YAML),
these tests RUN the actual bash/jq snippets embedded in ``GATE_POLL_SCRIPT``
against fixture JSON, exercising two behaviours that only exist at runtime:

1. **Latest-per-name check-run dedup (1.1).** A retried CI run creates a
   brand-new check-run with the same name as the old one. The gate must
   collapse same-named check-runs to the newest (max ``started_at``, numeric
   ``id`` as tiebreak) BEFORE deciding pending/failure, so a stale failure
   from a superseded run cannot block the gate forever.

2. **Marker-first SHA resolution (1.3).** The gate resolves the release
   commit from a ``<!-- rlsbl-ci-sha: <40-hex> -->`` marker in the GitHub
   Release body (written by rlsbl at release time), falling back to
   ``$GITHUB_SHA`` when the marker is absent (older releases). The tag is
   ``inputs.tag`` (TAG_INPUT) when dispatched with an override, else the
   ref name.

The snippets are extracted verbatim from ``GATE_POLL_SCRIPT`` so the tests
track the shipped code rather than a copy of it.
"""

import json
import os
import shutil
import subprocess
import textwrap

import pytest

from rlsbl.publish_gate import GATE_POLL_SCRIPT

requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="requires jq and bash on PATH",
)


def _extract_block(start_marker: str, end_marker: str) -> str:
    """Return the verbatim slice of GATE_POLL_SCRIPT between two markers.

    Used to pull the actual ``runs=``/``pending=``/``not_success=`` shell
    assignments out of the shipped script so tests run the real code.
    """
    s = GATE_POLL_SCRIPT.index(start_marker)
    e = GATE_POLL_SCRIPT.index(end_marker, s) + len(end_marker)
    return GATE_POLL_SCRIPT[s:e]


RUNS_BLOCK = _extract_block('runs="$(jq', '<<< "$resp")"')
PENDING_BLOCK = _extract_block('pending="$(jq', '<<< "$runs")"')
NOT_SUCCESS_BLOCK = _extract_block('not_success="$(jq', '<<< "$runs")"')


def _run_bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=full_env,
    )


def _dedup_runs(check_runs, regex="^(test)( \\(.*\\))?$", run_id="999"):
    """Run the script's real ``runs=`` dedup pipeline over fixture check-runs.

    ``check_runs`` is a list of check-run objects; they are wrapped in the
    ``{"check_runs": [...]}`` envelope the GitHub API returns (the script
    slurps paginated pages with ``jq -s``). Returns the parsed deduped array.
    """
    resp = json.dumps({"check_runs": check_runs})
    script = "\n".join(
        [
            "set -euo pipefail",
            f"CI_CHECK_REGEX={json.dumps(regex)}",
            f"GITHUB_RUN_ID={json.dumps(run_id)}",
            f"resp={json.dumps(resp)}",
            RUNS_BLOCK,
            'printf %s "$runs"',
        ]
    )
    proc = _run_bash(script)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _pending_count(runs) -> int:
    script = "\n".join(
        [
            "set -euo pipefail",
            f"runs={json.dumps(json.dumps(runs))}",
            PENDING_BLOCK,
            'printf %s "$pending"',
        ]
    )
    proc = _run_bash(script)
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout)


def _not_success(runs):
    script = "\n".join(
        [
            "set -euo pipefail",
            f"runs={json.dumps(json.dumps(runs))}",
            NOT_SUCCESS_BLOCK,
            'printf %s "$not_success"',
        ]
    )
    proc = _run_bash(script)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _cr(name, status, conclusion, id, started_at, run_id="12345"):
    """Build a check-run fixture object as the GitHub checks API returns it."""
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "id": id,
        "started_at": started_at,
        "details_url": f"https://github.com/o/r/actions/runs/{run_id}/job/{id}",
    }


@requires_jq
class TestCheckRunDedup:
    def test_old_failure_new_success_proceeds(self):
        """(a) A retried run: same name, old failure superseded by a newer
        success. Dedup keeps only the success, so the gate proceeds."""
        runs = _dedup_runs(
            [
                _cr("test", "completed", "failure", 100, "2026-07-16T10:00:00Z"),
                _cr("test", "completed", "success", 200, "2026-07-16T11:00:00Z"),
            ]
        )
        assert len(runs) == 1
        assert runs[0]["conclusion"] == "success"
        assert _pending_count(runs) == 0
        assert _not_success(runs) == []

    def test_newest_running_waits(self):
        """(b) Newest same-named run is still in progress (older one is a
        stale failure). Dedup keeps the running one -> gate waits."""
        runs = _dedup_runs(
            [
                _cr("test", "completed", "failure", 100, "2026-07-16T10:00:00Z"),
                _cr("test", "in_progress", None, 200, "2026-07-16T11:00:00Z"),
            ]
        )
        assert len(runs) == 1
        assert runs[0]["status"] == "in_progress"
        assert _pending_count(runs) == 1

    def test_newest_failed_blocks(self):
        """(c) Newest same-named run failed (older one succeeded). Dedup keeps
        the failure -> gate blocks, and the script explains failure."""
        runs = _dedup_runs(
            [
                _cr("test", "completed", "success", 100, "2026-07-16T10:00:00Z"),
                _cr("test", "completed", "failure", 200, "2026-07-16T11:00:00Z"),
            ]
        )
        assert len(runs) == 1
        assert runs[0]["conclusion"] == "failure"
        blocked = _not_success(runs)
        assert len(blocked) == 1
        assert blocked[0]["conclusion"] == "failure"
        # The per-conclusion explanation is preserved.
        assert "failure|timed_out" in GATE_POLL_SCRIPT

    def test_id_tiebreak_when_started_at_equal(self):
        """Equal started_at -> higher numeric id wins (retry within the same
        second still resolves deterministically to the latest check-run)."""
        runs = _dedup_runs(
            [
                _cr("test", "completed", "failure", 100, "2026-07-16T10:00:00Z"),
                _cr("test", "completed", "success", 101, "2026-07-16T10:00:00Z"),
            ]
        )
        assert len(runs) == 1
        assert runs[0]["id"] == 101
        assert runs[0]["conclusion"] == "success"

    def test_distinct_names_all_kept(self):
        """Dedup is per-name: distinct matrix legs are all retained."""
        runs = _dedup_runs(
            [
                _cr("test (20)", "completed", "success", 1, "2026-07-16T10:00:00Z"),
                _cr("test (22)", "completed", "success", 2, "2026-07-16T10:00:00Z"),
            ]
        )
        assert len(runs) == 2
        assert {r["name"] for r in runs} == {"test (20)", "test (22)"}


# --- Marker-first SHA resolution (1.3) -------------------------------------

# The commit-resolution prologue runs before the poll loop (before now()).
RESOLUTION_PROLOGUE = GATE_POLL_SCRIPT.split("now() {", 1)[0]


def _write_fake_gh(dir_path, body_for_tag: dict):
    """Write a fake ``gh`` that answers ``release view <tag> --json body``.

    ``body_for_tag`` maps a tag string to the release body string to emit.
    A tag absent from the map makes the fake ``gh`` exit non-zero (as the
    real gh does when the release does not exist). Records invocations.
    """
    gh_path = os.path.join(dir_path, "gh")
    mapping = json.dumps(body_for_tag)
    script = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        echo "$@" >> "{dir_path}/gh_calls.log"
        if [ "$1" = "release" ] && [ "$2" = "view" ]; then
          tag="$3"
          python3 - "$tag" <<'PY'
        import json, sys
        mapping = json.loads({mapping!r})
        tag = sys.argv[1]
        if tag in mapping:
            print(mapping[tag], end="")
            sys.exit(0)
        sys.exit(1)
        PY
          exit $?
        fi
        exit 1
        """
    )
    with open(gh_path, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(gh_path, 0o755)
    return gh_path


def _resolve_sha(tmp_path, body_for_tag, env):
    """Run the resolution prologue with a fake gh and return resolved sha."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_fake_gh(str(bindir), body_for_tag)
    script = RESOLUTION_PROLOGUE + '\nprintf "RESOLVED=%s\\n" "$sha"\n'
    run_env = dict(env)
    run_env["PATH"] = f"{bindir}{os.pathsep}" + os.environ["PATH"]
    run_env.setdefault("CI_CHECK_REGEX", "^(test)$")
    proc = _run_bash(script, run_env)
    assert proc.returncode == 0, proc.stderr
    line = [
        ln for ln in proc.stdout.splitlines() if ln.startswith("RESOLVED=")
    ]
    assert line, proc.stdout + proc.stderr
    return line[0][len("RESOLVED=") :], proc


@requires_jq
class TestMarkerShaResolution:
    MARKER_SHA = "a" * 40
    FALLBACK_SHA = "b" * 40

    def test_marker_present_uses_marker_sha(self, tmp_path):
        """(d) Release body carries the marker -> the marker SHA is used,
        not $GITHUB_SHA."""
        body = f"Release notes\n\n<!-- rlsbl-ci-sha: {self.MARKER_SHA} -->\n"
        sha, _ = _resolve_sha(
            tmp_path,
            {"v1.2.3": body},
            {
                "GITHUB_REF_NAME": "v1.2.3",
                "GITHUB_SHA": self.FALLBACK_SHA,
            },
        )
        assert sha == self.MARKER_SHA

    def test_marker_absent_falls_back_to_github_sha(self, tmp_path):
        """(e) Older release, no marker in the body -> fall back to
        $GITHUB_SHA."""
        body = "Old release notes with no marker.\n"
        sha, _ = _resolve_sha(
            tmp_path,
            {"v1.2.3": body},
            {
                "GITHUB_REF_NAME": "v1.2.3",
                "GITHUB_SHA": self.FALLBACK_SHA,
            },
        )
        assert sha == self.FALLBACK_SHA

    def test_no_release_falls_back_to_github_sha(self, tmp_path):
        """gh release view failing (release absent) -> fall back."""
        sha, _ = _resolve_sha(
            tmp_path,
            {},  # no release for any tag
            {
                "GITHUB_REF_NAME": "v9.9.9",
                "GITHUB_SHA": self.FALLBACK_SHA,
            },
        )
        assert sha == self.FALLBACK_SHA

    def test_dispatch_at_tag_uses_tag_input(self, tmp_path):
        """(f) workflow_dispatch with an explicit TAG_INPUT override resolves
        the marker from THAT tag's release, not GITHUB_REF_NAME."""
        marker = "c" * 40
        body = f"<!-- rlsbl-ci-sha: {marker} -->"
        sha, proc = _resolve_sha(
            tmp_path,
            {"v2.0.0": body},
            {
                # ref_name points elsewhere; TAG_INPUT overrides it.
                "GITHUB_REF_NAME": "main",
                "GITHUB_SHA": self.FALLBACK_SHA,
                "TAG_INPUT": "v2.0.0",
            },
        )
        assert sha == marker
        # The fake gh was queried for the TAG_INPUT tag, not the ref name.
        calls = (tmp_path / "bin" / "gh_calls.log").read_text()
        assert "release view v2.0.0" in calls
        assert "release view main" not in calls
