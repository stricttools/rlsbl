"""A repair that creates the NEWEST release's GitHub Release gives it the Latest badge.

The decided rule: every real release keeps GitHub's default, "so the most
recent release shows as Latest", and the repair commands pass
``--latest=false`` so they never move the badge onto an OLD release. A repair
is also how a release whose own Release step failed gets its Release: the
release flow names ``rlsbl release reconcile`` and ``rlsbl monorepo mirror``
as the healers. Passing ``--latest=false`` there too left the most recent
release without the badge -- the badge stayed on the release before it -- which
is the opposite of the rule's purpose.

So a repair takes the badge exactly when the Release it creates is newer than
the repository's current Latest (or the repository has none), and keeps
``--latest=false`` otherwise, including whenever that cannot be established.
"Newer" is history in the repository's own tags (in a monorepo the Latest can
be another package's, whose version says nothing about recency) and the
version on a mirror (one package, whose history rlsbl does not hold locally).
"""

import subprocess

from rlsbl.release_file import write_archived_release_file

TREE = "f" * 40


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(root):
    """A repository with v1.0.0 on its first commit and v1.1.0 on its second."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.local")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    shas = {}
    for version in ("1.0.0", "1.1.0"):
        (root / "VERSION").write_text(version + "\n")
        _git(root, "add", "VERSION")
        _git(root, "commit", "-q", "-m", f"v{version}")
        _git(root, "tag", f"v{version}")
        shas[version] = _git(root, "rev-parse", "HEAD")
    return shas


class _Gh:
    """Records gh argv; answers the Latest query from *latest*.

    *latest* is a tag, None for a repository without releases (gh answers
    "release not found"), or an exception for an unanswered question.
    """

    def __init__(self, latest):
        self.latest = latest
        self.calls = []

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["release", "view"] and args[2].startswith("--"):
            if isinstance(self.latest, Exception):
                raise self.latest
            if self.latest is None:
                raise subprocess.CalledProcessError(
                    1, "gh release view", stderr="release not found\n",
                )
            return self.latest
        if args[:2] == ["release", "view"]:
            raise subprocess.CalledProcessError(1, "gh release view")
        return ""

    def create(self):
        (create,) = [c for c in self.calls if c[:2] == ["release", "create"]]
        return create


class _Ctx:
    def __init__(self, root):
        self.config = {}
        self.project_root = root
        self.workspace_root = None


class _Item:
    def __init__(self, data):
        self.data = data


def _reconcile(tmp_path, monkeypatch, *, version, latest):
    from rlsbl.commands.release_reconcile import RefAction, apply_item

    root = tmp_path / "repo"
    shas = _repo(root)
    monkeypatch.chdir(root)
    releases = root / ".rlsbl" / "releases"
    for v, sha in shas.items():
        write_archived_release_file(
            str(releases), v, bump="patch", include=["plain"],
            description="d", candidate_sha=sha, tree_hashes={".": TREE},
        )
    gh = _Gh(latest)
    apply_item(
        _Item(RefAction(kind="release", tag=f"v{version}", version=version,
                        target=shas[version])),
        ctx=_Ctx(str(root)), releases_dir=str(releases), changelog_path=None,
        push_timeout=30, gh=gh, log=lambda *_: None,
    )
    return gh.create()


class TestTheReconcile:

    def test_the_newest_release_takes_the_badge(self, tmp_path, monkeypatch):
        """v1.1.0's own Release step failed; the Latest is still v1.0.0."""
        create = _reconcile(tmp_path, monkeypatch, version="1.1.0", latest="v1.0.0")
        assert "--verify-tag" in create
        assert not [a for a in create if a.startswith("--latest")]

    def test_the_first_release_takes_the_badge(self, tmp_path, monkeypatch):
        create = _reconcile(tmp_path, monkeypatch, version="1.0.0", latest=None)
        assert not [a for a in create if a.startswith("--latest")]

    def test_an_older_release_never_moves_the_badge(self, tmp_path, monkeypatch):
        create = _reconcile(tmp_path, monkeypatch, version="1.0.0", latest="v1.1.0")
        assert "--latest=false" in create

    def test_an_unanswered_latest_never_moves_the_badge(self, tmp_path, monkeypatch):
        create = _reconcile(
            tmp_path, monkeypatch, version="1.1.0",
            latest=subprocess.CalledProcessError(1, "gh", stderr="HTTP 502"),
        )
        assert "--latest=false" in create

    def test_a_latest_from_unrelated_history_never_moves_the_badge(
        self, tmp_path, monkeypatch,
    ):
        """A Latest whose tag this checkout lacks says nothing about recency."""
        create = _reconcile(tmp_path, monkeypatch, version="1.1.0", latest="other@v9.0.0")
        assert "--latest=false" in create


class TestTheScrubsReleaseRewrite:

    def test_the_newest_release_takes_the_badge(self, tmp_path, monkeypatch):
        from rlsbl.commands.release_reconcile import update_github_releases

        root = tmp_path / "repo"
        shas = _repo(root)
        monkeypatch.chdir(root)
        write_archived_release_file(
            str(root / ".rlsbl" / "releases"), "1.1.0", bump="patch",
            include=["plain"], description="d", candidate_sha=shas["1.1.0"],
            tree_hashes={".": TREE},
        )
        (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
        gh = _Gh("v1.0.0")
        update_github_releases(
            [{"refname": "refs/tags/v1.1.0"}], ctx=_Ctx(str(root)),
            project_root=str(root), workspace_projects=None,
            tag_prefix_index=None, gh=gh, gh_installed=lambda: True,
            gh_auth=lambda: True, extract_entry=lambda _p, _v: "- A change.\n",
        )
        assert not [a for a in gh.create() if a.startswith("--latest")]


class TestTheMirror:

    def _apply(self, monkeypatch, *, version, latest):
        from rlsbl.commands.monorepo import mirror_cmd

        seen = {}

        def fake_publish_version(**kwargs):
            seen.update(kwargs)
            return "s", "pushed", "created"

        gh = _Gh(latest)
        monkeypatch.setattr(
            "rlsbl.mirror_publication.publish_version", fake_publish_version,
        )
        monkeypatch.setattr(
            "rlsbl.utils.run_gh_unscoped", lambda args, **kw: gh(args),
        )

        class Plan:
            state = "missing"
            tag = f"v{version}"
            release_commit_sha = "a" * 40
            notes = ""
            reason = None

        Plan.version = version
        mirror_cmd._apply_tag(Plan(), "o/mirror", ".", "packages/lib",
                              notes_dir=".")
        # The Latest was asked of the MIRROR.
        (query,) = [c for c in gh.calls if c[:2] == ["release", "view"]]
        assert query[query.index("--repo") + 1] == "o/mirror"
        return seen["moves_latest"]

    def test_the_mirrors_newest_version_takes_the_badge(self, monkeypatch):
        assert self._apply(monkeypatch, version="1.1.0", latest="v1.0.0") is True

    def test_the_mirrors_first_version_takes_the_badge(self, monkeypatch):
        assert self._apply(monkeypatch, version="1.0.0", latest=None) is True

    def test_an_older_version_never_moves_the_mirrors_badge(self, monkeypatch):
        assert self._apply(monkeypatch, version="1.0.0", latest="v1.1.0") is False

    def test_an_unanswered_latest_never_moves_the_mirrors_badge(self, monkeypatch):
        assert self._apply(
            monkeypatch, version="1.1.0",
            latest=subprocess.CalledProcessError(1, "gh", stderr="HTTP 502"),
        ) is False
