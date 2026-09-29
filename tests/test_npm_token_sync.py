"""``rlsbl secrets sync-npm-token`` and the ``npm-token-synced`` check.

The failure class: the npm token in ``~/.npmrc`` is replaced (it expired or was
rotated), every repository's ``NPM_TOKEN`` Actions secret still holds the old
one, and the next release's CI npm publish fails against the registry after
the release has already tagged and pushed.

No test here contacts npm or GitHub, and no real secret is read: ``~/.npmrc``
is a file under ``tmp_path``, the registry is a stubbed ``effects.urlopen``, and
``gh``/``npm`` are stubbed at the ``rlsbl._effects_direct.run`` seam (or at the
module's own injectable functions).
"""

import io
import json
import subprocess
import urllib.error
from types import SimpleNamespace

import pytest

import rlsbl
from rlsbl import _effects_direct, effects, npm_token
from rlsbl.npm_token import (
    NpmTokenError,
    SYNC_COMMAND,
    evaluate_npm_token_sync,
    npm_whoami,
    read_npmrc_token,
    sync_npm_token,
    token_created_at,
)

from conftest import make_ctx

TOKEN = "npm_AbCdEfGh0123456789abcdefghijklmnWXYZ"
OTHER = "npm_ZzZzZzZz0000000000000000000000000000QQQQ"
NPM_CI = {"npm": {"type": "npm", "local": False, "target": "npm"}}


def _config(**extra):
    config = {"publish_mode": "ci", "github_repo": "owner/repo",
              "pipelines": NPM_CI}
    config.update(extra)
    return config


def _npmrc(tmp_path, token=TOKEN):
    path = tmp_path / ".npmrc"
    path.write_text(
        "registry=https://registry.npmjs.org/\n"
        f"//registry.npmjs.org/:_authToken={token}\n"
    )
    return str(path)


def _listing(*entries):
    """``npm token list --json`` output: (token, created, revoked) triples."""
    return json.dumps([
        {
            "token": f"{tok[:8]}...{tok[-4:]}",
            "key": "0" * 36,
            "created": created,
            "revoked": revoked,
        }
        for tok, created, revoked in entries
    ])


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Reading the local token
# ---------------------------------------------------------------------------


class TestReadNpmrcToken:
    def test_the_registry_token_line_is_read(self, tmp_path):
        assert read_npmrc_token(_npmrc(tmp_path)) == TOKEN

    def test_a_missing_file_refuses_naming_npm_login(self, tmp_path):
        with pytest.raises(NpmTokenError) as exc:
            read_npmrc_token(str(tmp_path / "absent"))
        assert "npm login" in str(exc.value)

    def test_a_file_without_the_line_refuses(self, tmp_path):
        path = tmp_path / ".npmrc"
        path.write_text("registry=https://registry.npmjs.org/\n")
        with pytest.raises(NpmTokenError) as exc:
            read_npmrc_token(str(path))
        assert "_authToken" in str(exc.value)

    def test_an_environment_variable_reference_is_not_a_token(self, tmp_path):
        with pytest.raises(NpmTokenError):
            read_npmrc_token(_npmrc(tmp_path, token="${NPM_TOKEN}"))


# ---------------------------------------------------------------------------
# Asking npm
# ---------------------------------------------------------------------------


class TestNpmWhoami:
    def test_the_token_travels_in_a_header_and_the_user_comes_back(
        self, monkeypatch,
    ):
        seen = []

        def fake_urlopen(request, *, timeout=None):
            seen.append(request)
            return _Response(b'{"username": "someone"}')

        monkeypatch.setattr(effects, "urlopen", fake_urlopen)
        assert npm_whoami(TOKEN) == "someone"
        (request,) = seen
        assert request.full_url == "https://registry.npmjs.org/-/whoami"
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        assert TOKEN not in request.full_url

    @pytest.mark.parametrize("code", [401, 403])
    def test_a_refused_token_names_npm_login_and_the_sync(self, monkeypatch, code):
        def fake_urlopen(request, *, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, code, "Unauthorized", {}, None,
            )

        monkeypatch.setattr(effects, "urlopen", fake_urlopen)
        with pytest.raises(NpmTokenError) as exc:
            npm_whoami(TOKEN)
        assert "npm login" in str(exc.value)
        assert SYNC_COMMAND in str(exc.value)
        assert TOKEN not in str(exc.value)

    def test_an_unreachable_registry_is_an_error_not_a_pass(self, monkeypatch):
        def fake_urlopen(request, *, timeout=None):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(effects, "urlopen", fake_urlopen)
        with pytest.raises(NpmTokenError):
            npm_whoami(TOKEN)


class TestTokenCreatedAt:
    def _stub_npm(self, monkeypatch, stdout, returncode=0):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return _completed(argv, returncode, stdout, "boom")

        monkeypatch.setattr(effects, "run", fake_run)
        return calls

    def test_the_matching_unrevoked_token_gives_the_time(self, monkeypatch):
        calls = self._stub_npm(monkeypatch, _listing(
            (OTHER, "2026-01-01T00:00:00.000Z", None),
            (TOKEN, "2026-09-16T19:22:51.030Z", None),
        ))
        created = token_created_at(TOKEN)
        assert created.isoformat() == "2026-09-16T19:22:51.030000+00:00"
        assert calls == [["npm", "token", "list", "--json"]]

    def test_no_matching_token_cannot_be_determined(self, monkeypatch):
        self._stub_npm(monkeypatch, _listing(
            (OTHER, "2026-01-01T00:00:00.000Z", None),
        ))
        with pytest.raises(NpmTokenError) as exc:
            token_created_at(TOKEN)
        assert "0 unrevoked tokens" in str(exc.value)

    def test_a_revoked_match_does_not_count(self, monkeypatch):
        self._stub_npm(monkeypatch, _listing(
            (TOKEN, "2026-09-16T19:22:51.030Z", "2026-09-20T00:00:00.000Z"),
        ))
        with pytest.raises(NpmTokenError):
            token_created_at(TOKEN)

    def test_a_failing_listing_is_an_error(self, monkeypatch):
        self._stub_npm(monkeypatch, "", returncode=1)
        with pytest.raises(NpmTokenError) as exc:
            token_created_at(TOKEN)
        assert "exited 1" in str(exc.value)


# ---------------------------------------------------------------------------
# The check's evaluation
# ---------------------------------------------------------------------------


CREATED = "2026-09-16T19:22:51.030Z"


def _evaluate(tmp_path, *, updated_at="2026-09-29T18:21:00Z", status="present",
              config=None, whoami=None, created_at=None):
    from rlsbl.npm_token import parse_timestamp

    return evaluate_npm_token_sync(
        config or _config(), "owner/repo",
        npmrc=_npmrc(tmp_path),
        whoami=whoami or (lambda token: "someone"),
        created_at=created_at or (lambda token: parse_timestamp(CREATED)),
        probe=lambda slug, name: (
            {"status": status, "updated_at": updated_at}
            if status == "present" else {"status": status, "message": "nope"}
        ),
    )


class TestEvaluateNpmTokenSync:
    def test_a_secret_set_after_the_token_was_created_passes(self, tmp_path):
        verdict = _evaluate(tmp_path)
        assert verdict.ok and verdict.skip_reason is None

    def test_a_secret_set_before_the_token_was_created_fails_naming_the_sync(
        self, tmp_path,
    ):
        verdict = _evaluate(tmp_path, updated_at="2026-08-24T09:22:07Z")
        assert not verdict.ok
        (problem,) = verdict.problems
        assert SYNC_COMMAND in problem
        assert "owner/repo" in problem
        assert TOKEN not in problem

    def test_an_unreadable_secret_fails_rather_than_passing(self, tmp_path):
        assert not _evaluate(tmp_path, status="unknown").ok

    def test_a_secret_without_an_update_time_fails(self, tmp_path):
        assert not _evaluate(tmp_path, updated_at=None).ok

    def test_an_absent_secret_fails(self, tmp_path):
        assert not _evaluate(tmp_path, status="absent").ok

    def test_an_undeterminable_creation_time_fails(self, tmp_path):
        def cannot(token):
            raise NpmTokenError("no match")

        verdict = _evaluate(tmp_path, created_at=cannot)
        assert not verdict.ok
        assert "no match" in verdict.problems[0]

    def test_a_token_npm_refuses_fails(self, tmp_path):
        def refused(token):
            raise NpmTokenError(f"refused; run `{SYNC_COMMAND}`")

        assert not _evaluate(tmp_path, whoami=refused).ok

    def test_a_project_without_an_npm_ci_publish_is_skipped(self, tmp_path):
        verdict = _evaluate(tmp_path, config=_config(pipelines={
            "pypi": {"type": "pypi", "local": False, "target": "pypi"},
        }))
        assert verdict.skip_reason is not None

    def test_a_project_publishing_nothing_is_skipped(self, tmp_path):
        verdict = _evaluate(tmp_path, config=_config(publish_mode="none"))
        assert verdict.skip_reason is not None


# ---------------------------------------------------------------------------
# The registered check, and the truthfulness of the fix it names
# ---------------------------------------------------------------------------


class _FakeWorld:
    """npm and GitHub as the check and the command see them, in memory.

    ``secrets`` maps a repository to its NPM_TOKEN ``(value, updated_at)``;
    ``gh secret set`` writes it, with the time moving past the token's creation.
    """

    def __init__(self, secrets, archived=()):
        self.secrets = dict(secrets)
        self.archived = set(archived)
        self.set_calls = []

    def run(self, argv, **kwargs):
        argv = list(argv)
        if argv[:4] == ["npm", "token", "list", "--json"]:
            return _completed(argv, 0, _listing((TOKEN, CREATED, None)))
        if argv[:1] != ["gh"]:
            return _effects_direct_run(argv, **kwargs)
        args = argv[1:]
        if args[:3] == ["api", "--method", "GET"]:
            path = args[3]
            if path.startswith("repos/") and "/actions/secrets/" in path:
                slug = path[len("repos/"):].split("/actions/")[0]
                if slug not in self.secrets:
                    return _completed(argv, 1, "", "gh: Not Found (HTTP 404)")
                _value, updated = self.secrets[slug]
                return _completed(argv, 0, json.dumps({
                    "name": "NPM_TOKEN", "updated_at": updated,
                }))
            if path == "user":
                return _completed(argv, 0, "me\n")
            if path.startswith("user/orgs"):
                return _completed(argv, 0, "org\n")
            if path.startswith("user/repos"):
                return _completed(argv, 0, self._rows("me/"))
            if path.startswith("orgs/org/repos"):
                return _completed(argv, 0, self._rows("org/"))
        if args[:3] == ["secret", "set", "NPM_TOKEN"]:
            slug = args[args.index("--repo") + 1]
            self.set_calls.append((slug, kwargs.get("input"), argv))
            self.secrets[slug] = (kwargs.get("input"), "2026-09-30T00:00:00Z")
            return _completed(argv, 0, "")
        raise AssertionError(f"unexpected gh call: {argv}")

    def _rows(self, owner_prefix):
        names = sorted(
            {n for n in self.secrets if n.startswith(owner_prefix)}
            | {f"{owner_prefix}plain"}
            | {n for n in self.archived if n.startswith(owner_prefix)}
        )
        return "".join(
            f"{n}\t{'true' if n in self.archived else 'false'}\n" for n in names
        )


_effects_direct_run = _effects_direct.run


@pytest.fixture
def npm_home(tmp_path, monkeypatch):
    """HOME with a ~/.npmrc holding TOKEN, and npm accepting it."""
    home = tmp_path / "home"
    home.mkdir()
    _npmrc(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        effects, "urlopen",
        lambda request, *, timeout=None: _Response(b'{"username": "someone"}'),
    )
    return home


def _check(tmp_path):
    return rlsbl.app._check_defs["npm-token-synced"].impl(
        make_ctx(tmp_path, _config()),
    )


class TestRegisteredCheck:
    def test_a_stale_secret_fails_and_running_the_named_fix_clears_it(
        self, tmp_path, npm_home, monkeypatch,
    ):
        world = _FakeWorld({"owner/repo": ("old", "2026-08-24T09:22:07Z")})
        monkeypatch.setattr(_effects_direct, "run", world.run)

        result = _check(tmp_path)
        assert result.status == "fail"
        text = " ".join(p.text for p in result.problems)
        assert SYNC_COMMAND in text

        code = sync_npm_token(
            all_repositories=False, origin_repo="owner/repo",
            out=io.StringIO(), err=io.StringIO(),
        )
        assert code == 0
        assert _check(tmp_path).status == "pass"

    def test_it_is_declared_for_the_release_preflight(self):
        from rlsbl.checks import hook_selection

        meta = rlsbl.app._check_defs["npm-token-synced"]
        assert hook_selection("pre-release") in meta.tags
        assert meta.severity == "error"


# ---------------------------------------------------------------------------
# rlsbl secrets sync-npm-token
# ---------------------------------------------------------------------------


def _git_checkout(path, slug="owner/repo"):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin",
         f"git@github.com:{slug}.git"],
        check=True,
    )
    return path


class TestSyncCommand:
    def test_the_current_repository_is_set_from_stdin(
        self, tmp_path, npm_home, monkeypatch,
    ):
        world = _FakeWorld({"owner/repo": ("old", "2026-08-24T09:22:07Z")})
        monkeypatch.setattr(_effects_direct, "run", world.run)
        monkeypatch.chdir(_git_checkout(tmp_path / "repo"))

        result = rlsbl.app.test(
            ["secrets", "sync-npm-token", "--approve-consequential"],
        )

        assert result.exit_code == 0, result.stderr
        ((slug, piped, argv),) = world.set_calls
        assert slug == "owner/repo"
        assert piped == TOKEN, "the token must reach gh on its stdin"
        assert TOKEN not in " ".join(argv), "the token must never be an argument"
        assert "owner/repo: NPM_TOKEN set" in result.stdout
        assert "someone" in result.stdout
        assert TOKEN not in result.stdout + result.stderr

    def test_a_repository_without_the_secret_is_refused_not_created(
        self, tmp_path, npm_home, monkeypatch,
    ):
        world = _FakeWorld({})
        monkeypatch.setattr(_effects_direct, "run", world.run)
        monkeypatch.chdir(_git_checkout(tmp_path / "repo"))

        result = rlsbl.app.test(
            ["secrets", "sync-npm-token", "--approve-consequential"],
        )

        assert result.exit_code == 1
        assert world.set_calls == []
        assert "never creates" in result.stderr

    def test_a_token_npm_refuses_is_refused_before_anything_is_set(
        self, tmp_path, npm_home, monkeypatch,
    ):
        world = _FakeWorld({"owner/repo": ("old", "2026-08-24T09:22:07Z")})
        monkeypatch.setattr(_effects_direct, "run", world.run)

        def refused(request, *, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

        monkeypatch.setattr(effects, "urlopen", refused)
        monkeypatch.chdir(_git_checkout(tmp_path / "repo"))

        result = rlsbl.app.test(
            ["secrets", "sync-npm-token", "--approve-consequential"],
        )

        assert result.exit_code == 1
        assert world.set_calls == []
        assert "npm login" in result.stderr

    def test_a_missing_npmrc_token_is_refused(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(_git_checkout(tmp_path / "repo"))

        result = rlsbl.app.test(
            ["secrets", "sync-npm-token", "--approve-consequential"],
        )

        assert result.exit_code == 1
        assert ".npmrc" in result.stderr

    def test_all_sets_only_repositories_that_carry_the_secret(
        self, tmp_path, npm_home, monkeypatch,
    ):
        world = _FakeWorld(
            {
                "me/tool": ("old", "2026-08-24T09:22:07Z"),
                "org/lib": ("old", "2026-08-27T13:20:07Z"),
            },
            archived={"org/old"},
        )
        monkeypatch.setattr(_effects_direct, "run", world.run)
        monkeypatch.chdir(tmp_path)

        result = rlsbl.app.test(
            ["secrets", "sync-npm-token", "--all", "--approve-consequential"],
        )

        assert result.exit_code == 0, result.stderr
        assert sorted(s for s, _t, _a in world.set_calls) == ["me/tool", "org/lib"]
        assert all(piped == TOKEN for _s, piped, _a in world.set_calls)
        assert "me/tool: NPM_TOKEN set" in result.stdout
        assert "org/lib: NPM_TOKEN set" in result.stdout
        assert "org/old: skipped: archived" in result.stdout
        assert "me/plain" not in world.secrets
        assert TOKEN not in result.stdout + result.stderr

    def test_a_dry_run_sets_nothing_and_never_renders_the_token(
        self, tmp_path, npm_home, monkeypatch,
    ):
        world = _FakeWorld({"owner/repo": ("old", "2026-08-24T09:22:07Z")})
        monkeypatch.setattr(
            npm_token, "probe_repo_secret",
            lambda slug, name: {"status": "present", "updated_at": "x"},
        )
        monkeypatch.setattr(_effects_direct, "run", world.run)
        monkeypatch.chdir(_git_checkout(tmp_path / "repo"))

        result = rlsbl.app.test(["--dry-run", "secrets", "sync-npm-token"])

        assert result.exit_code == 0, result.stderr
        assert world.set_calls == []
        assert "owner/repo: would set NPM_TOKEN" in result.stdout
        assert "gh secret set NPM_TOKEN --repo owner/repo" in result.stdout
        assert TOKEN not in result.stdout + result.stderr

    def test_it_is_consequential(self):
        group = rlsbl.app._groups["secrets"]
        assert group.commands["sync-npm-token"].consequential is True


def test_the_argv_never_carries_the_token(monkeypatch):
    """The one write goes through gh's stdin, never an argument."""
    seen = {}

    def fake_gh(args, **kwargs):
        seen["args"] = args
        seen["input"] = kwargs.get("input")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(effects, "gh", fake_gh)
    assert npm_token.set_npm_token_secret("o/r", TOKEN) == ("set", "")
    assert TOKEN not in seen["args"]
    assert seen["input"] == TOKEN
