"""The publish gate stops at its own deadline even when the checks API keeps failing.

The poll loop's branch for a failed checks-API request used to sleep and retry
without looking at the deadline, so a rate limit or an endpoint that kept
answering 404 held the gate job until GitHub's six-hour job default -- billed
minutes on a private repository. Every generated gate job also carries its own
``timeout-minutes``, so the job ends shortly after the script's deadline even
if the script itself hangs.
"""

import os
import shutil
import subprocess

import pytest

from rlsbl.publish_gate import (
    GATE_TIMEOUT_MINUTES,
    GATE_POLL_SCRIPT,
    build_gate_job,
    build_router_gate_job,
    gate_job_template_snippet,
)

requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="requires bash on PATH",
)


@requires_bash
def test_a_checks_api_that_keeps_failing_ends_at_the_deadline(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text("#!/bin/sh\necho 'HTTP 404: Not Found' >&2\nexit 1\n")
    gh.chmod(0o755)

    env = dict(os.environ)
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "GATE_TIMEOUT_MINUTES": "0",
        "GATE_GRACE_MINUTES": "0",
        "GATE_POLL_SECONDS": "0",
        "GATE_MARKER_ATTEMPTS": "1",
        "GATE_MARKER_RETRY_SECONDS": "0",
        "CI_CHECK_REGEX": "^(test)$",
        "GITHUB_REF_NAME": "v1.0.0",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "1",
    })
    try:
        proc = subprocess.run(
            ["bash", "-c", GATE_POLL_SCRIPT], capture_output=True, text=True,
            env=env, timeout=20,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the gate kept retrying a failing checks API past its deadline")
    assert proc.returncode == 1
    assert "::error::" in proc.stdout
    assert "checks API" in proc.stdout


def _timeout(job):
    return job["timeout-minutes"]


def test_every_gate_job_has_its_own_timeout():
    expected = int(GATE_TIMEOUT_MINUTES) + 5
    assert _timeout(build_gate_job(check_regex="^(test)$")) == expected
    assert _timeout(build_router_gate_job([("pkg@v", "^(x)$")])) == expected
    assert f"timeout-minutes: {expected}" in gate_job_template_snippet("^(test)$")
