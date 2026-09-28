"""The workflows a published Release starts: the preflight and the start confirmation.

GitHub starts nothing, and reports no error, when the tagged commit lacks the
workflow file, the workflow or GitHub Actions is disabled, or the Release was
created with a workflow's ``GITHUB_TOKEN`` (all measured against a real
repository). The preflight refuses the knowable ones before a release pushes;
the confirmation requires a run for the tag afterwards. Every refusal's fix is
applied here and seen to clear it.
"""

import json
import subprocess

import pytest

from rlsbl.publish_workflows import (
    PublishWorkflowError,
    ReleaseWorkflow,
    confirm_runs_started,
    preflight,
    release_workflows,
)

TAG = "v1.0.0"

PUBLISH = """\
name: Publish
on:
  release:
    types: [published]
  workflow_dispatch:
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
"""
CI = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo test
"""


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(tmp_path, files):
    _git(tmp_path, "init", "-q", "-b", "main")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, text in files.items():
        (wf / name).write_text(text)
    (tmp_path / "README").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return _git(tmp_path, "rev-parse", "HEAD")


class FakeGitHub:
    """A gh double holding the repository state the probes read."""

    def __init__(self, *, enabled=True, states=None, runs=None, author="smm-h"):
        self.enabled = enabled
        self.states = dict(states or {})
        self.runs = dict(runs or {})
        self.author = author
        self.calls = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["release", "view"]:
            return self.author
        if args[:3] == ["api", "--method", "GET"]:
            path = args[3]
            if path.endswith("/actions/permissions"):
                return json.dumps({"enabled": self.enabled})
            if "/runs?" in path:
                filename = path.split("/actions/workflows/")[1].split("/runs")[0]
                return json.dumps({"workflow_runs": self.runs.get(filename, [])})
            if "/actions/workflows/" in path:
                filename = path.rsplit("/", 1)[1]
                if filename not in self.states:
                    raise subprocess.CalledProcessError(
                        1, "gh", stderr="gh: Not Found (HTTP 404)",
                    )
                return self.states[filename]
        raise AssertionError(f"unexpected gh call: {args}")

    # The fixes the refusals name, applied to the double.
    def run(self, command):
        if command == "gh workflow enable publish.yml":
            self.states["publish.yml"] = "active"
        elif command.startswith("gh api --method PUT") and "enabled=true" in command:
            self.enabled = True
        else:
            raise AssertionError(f"not a fix this double knows: {command}")


def _fix_in(message):
    return message.split("`")[1]


class TestWhichWorkflowsAReleaseStarts:

    def test_only_release_triggered_files(self, tmp_path):
        sha = _repo(tmp_path, {"publish.yml": PUBLISH, "ci.yml": CI})
        assert release_workflows(str(tmp_path), sha) == [
            ReleaseWorkflow(".github/workflows/publish.yml", ("published",)),
        ]

    @pytest.mark.parametrize("types,prerelease,starts", [
        (None, False, True),
        (("published",), True, True),
        (("released",), False, True),
        (("released",), True, False),
        (("prereleased",), True, True),
        (("edited",), False, False),
    ])
    def test_activity_types(self, types, prerelease, starts):
        w = ReleaseWorkflow(".github/workflows/x.yml", types)
        assert w.starts_for(prerelease=prerelease) is starts


class TestPreflight:

    def _preflight(self, tmp_path, sha, gh, publishes=True):
        preflight(git_root=str(tmp_path), commit=sha, gh=gh,
                  publishes=publishes, prerelease=False, slug="o/r")

    def test_a_ready_repository_passes(self, tmp_path):
        sha = _repo(tmp_path, {"publish.yml": PUBLISH})
        self._preflight(tmp_path, sha, FakeGitHub(states={"publish.yml": "active"}))

    def test_a_workflow_github_does_not_know_yet_passes(self, tmp_path):
        """A workflow this release adds is registered when the release pushes it."""
        sha = _repo(tmp_path, {"publish.yml": PUBLISH})
        self._preflight(tmp_path, sha, FakeGitHub())

    def test_no_publish_workflow_is_refused_until_scaffold_generates_it(
        self, tmp_path,
    ):
        sha = _repo(tmp_path, {"ci.yml": CI})
        gh = FakeGitHub()
        with pytest.raises(PublishWorkflowError) as exc:
            self._preflight(tmp_path, sha, gh)
        assert "rlsbl scaffold" in str(exc.value)
        # The fix: the scaffolded publish workflow, committed.
        (tmp_path / ".github" / "workflows" / "publish.yml").write_text(PUBLISH)
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "scaffold")
        self._preflight(tmp_path, _git(tmp_path, "rev-parse", "HEAD"), gh)

    def test_a_project_that_does_not_publish_needs_no_workflow(self, tmp_path):
        sha = _repo(tmp_path, {"ci.yml": CI})
        gh = FakeGitHub()
        self._preflight(tmp_path, sha, gh, publishes=False)
        assert gh.calls == []

    def test_a_disabled_workflow_is_refused_until_enabled(self, tmp_path):
        sha = _repo(tmp_path, {"publish.yml": PUBLISH})
        gh = FakeGitHub(states={"publish.yml": "disabled_manually"})
        with pytest.raises(PublishWorkflowError) as exc:
            self._preflight(tmp_path, sha, gh)
        gh.run(_fix_in(str(exc.value)))
        self._preflight(tmp_path, sha, gh)

    def test_disabled_actions_are_refused_until_enabled(self, tmp_path):
        sha = _repo(tmp_path, {"publish.yml": PUBLISH})
        gh = FakeGitHub(enabled=False, states={"publish.yml": "active"})
        with pytest.raises(PublishWorkflowError) as exc:
            self._preflight(tmp_path, sha, gh)
        gh.run(_fix_in(str(exc.value)))
        self._preflight(tmp_path, sha, gh)

    def test_an_unreadable_answer_is_a_refusal(self, tmp_path):
        sha = _repo(tmp_path, {"publish.yml": PUBLISH})

        def gh(args):
            raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 403")

        with pytest.raises(PublishWorkflowError, match="could not read"):
            self._preflight(tmp_path, sha, gh)


def _run(event="release", branch=TAG):
    return {"event": event, "head_branch": branch, "id": 1}


class TestTheStartConfirmation:

    def _confirm(self, tmp_path, sha, gh):
        confirm_runs_started(
            tag=TAG, sha=sha, prerelease=False, git_root=str(tmp_path), gh=gh,
            log=lambda *_: None, discovery_seconds=0, sleep=lambda _s: None,
        )

    def test_a_run_for_the_tag_confirms(self, tmp_path):
        sha = _repo(tmp_path, {"publish.yml": PUBLISH})
        self._confirm(tmp_path, sha, FakeGitHub(runs={"publish.yml": [_run()]}))

    def test_nothing_expected_asks_nothing(self, tmp_path):
        sha = _repo(tmp_path, {"ci.yml": CI})
        gh = FakeGitHub()
        self._confirm(tmp_path, sha, gh)
        assert gh.calls == []

    def test_a_run_for_another_tag_does_not_count(self, tmp_path):
        sha = _repo(tmp_path, {"publish.yml": PUBLISH})
        gh = FakeGitHub(states={"publish.yml": "active"},
                        runs={"publish.yml": [_run(branch="other@v2.0.0")]})
        with pytest.raises(PublishWorkflowError):
            self._confirm(tmp_path, sha, gh)

    def test_no_run_is_a_hard_error_naming_what_was_found(self, tmp_path):
        sha = _repo(tmp_path, {"publish.yml": PUBLISH})
        gh = FakeGitHub(states={"publish.yml": "disabled_manually"},
                        author="github-actions[bot]")
        with pytest.raises(PublishWorkflowError) as exc:
            self._confirm(tmp_path, sha, gh)
        message = str(exc.value)
        assert "no run of .github/workflows/publish.yml started" in message
        assert "github-actions[bot]" in message and "GITHUB_TOKEN" in message
        assert "gh workflow enable publish.yml" in message
        assert "rlsbl release retry" in message
        assert f"rlsbl watch {sha}" in message

    def test_the_retry_dispatch_clears_it(self, tmp_path):
        """`rlsbl release retry` dispatches the workflow at the tag, which is a
        run for the tag: the confirmation `rlsbl watch` repeats then passes."""
        sha = _repo(tmp_path, {"publish.yml": PUBLISH})
        gh = FakeGitHub(states={"publish.yml": "active"})
        with pytest.raises(PublishWorkflowError):
            self._confirm(tmp_path, sha, gh)
        gh.runs["publish.yml"] = [_run(event="workflow_dispatch")]
        self._confirm(tmp_path, sha, gh)
