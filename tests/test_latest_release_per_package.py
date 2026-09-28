""""The latest release" is the package's own newest release, read from its release record.

``rlsbl release yank`` and ``rlsbl release deprecate`` refuse the latest release
(``rlsbl release undo`` is for that), and ``rlsbl watch`` links the latest
release when it has no release of its own to name. All three used to ask GitHub
for the repository's newest Release -- in a monorepo that is whichever package
released last, so a package's own newest release could be yanked or deprecated,
and the link named another package. They now read the package's own release
record: its highest archived version that was released.
"""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from rlsbl.release_record import latest_released_version

OTHER_PACKAGES_RELEASE = "other@v9.0.0"


def _archive(releases, version, *, never_released=False):
    releases.mkdir(parents=True, exist_ok=True)
    text = (
        'format_version = 1\nbump = "patch"\ndescription = "d"\n'
        'include = []\nexclude = []\n'
    )
    if never_released:
        text += "never_released = true\n"
    (releases / f"v{version}.toml").write_text(text, encoding="utf-8")


class TestTheRecordAnswers:

    def test_the_highest_released_version(self, tmp_path):
        releases = tmp_path / "releases"
        _archive(releases, "0.9.1")
        _archive(releases, "0.10.0")
        _archive(releases, "0.11.0", never_released=True)
        assert latest_released_version(str(releases)) == "0.10.0"

    def test_no_release_yet(self, tmp_path):
        assert latest_released_version(str(tmp_path / "releases")) is None


def _gh(args, **kwargs):
    args = list(args)
    if args[:2] == ["release", "list"]:
        return OTHER_PACKAGES_RELEASE
    if args[:2] == ["release", "view"]:
        return "Old notes"
    return ""


@pytest.mark.parametrize("command", ["deprecate", "yank"])
def test_the_packages_own_newest_release_is_refused(command, tmp_path, monkeypatch):
    """GitHub's newest Release belongs to another package; this package's own
    newest release is still the one ``release undo`` exists for."""
    monkeypatch.chdir(tmp_path)
    _archive(tmp_path / ".rlsbl" / "releases", "0.9.1")
    mod = f"rlsbl.commands.{command}"
    module = __import__(mod, fromlist=["run_cmd"])
    with (
        patch(f"{mod}.check_gh_auth", return_value=True),
        patch(f"{mod}.check_gh_installed", return_value=True),
        patch(f"{mod}.find_workspace_root", return_value=None),
        patch(f"{mod}.resolve_member_context", return_value=MagicMock(targets=[])),
        patch(f"{mod}.run_gh", side_effect=_gh),
        patch("rlsbl.release_publication.commit_files"),
        patch("sys.stderr", new_callable=StringIO) as err,
        pytest.raises(SystemExit) as exc,
    ):
        module.run_cmd(["0.9.1"], {}, project_root=".")
    assert exc.value.code == 1
    assert "v0.9.1 is the latest release" in err.getvalue()
    assert "rlsbl release undo" in err.getvalue()


def test_the_refusal_clears_once_a_newer_release_exists(tmp_path, monkeypatch):
    """Following the refusal's own meaning: once the package has released a
    newer version, the older one can be deprecated."""
    monkeypatch.chdir(tmp_path)
    releases = tmp_path / ".rlsbl" / "releases"
    _archive(releases, "0.9.1")
    _archive(releases, "0.9.2")
    from rlsbl.commands import deprecate

    with (
        patch("rlsbl.commands.deprecate.check_gh_auth", return_value=True),
        patch("rlsbl.commands.deprecate.check_gh_installed", return_value=True),
        patch("rlsbl.commands.deprecate.find_workspace_root", return_value=None),
        patch("rlsbl.commands.deprecate.resolve_member_context",
              return_value=MagicMock(targets=[])),
        patch("rlsbl.commands.deprecate.run_gh", side_effect=_gh),
        patch("rlsbl.release_publication.commit_files"),
        patch("sys.stdout", new_callable=StringIO) as out,
    ):
        deprecate.run_cmd(["0.9.1"], {"dry-run": True}, project_root=".")
    assert "Would mark v0.9.1 as pre-release" in out.getvalue()


def test_watch_links_the_packages_own_latest_release(tmp_path, monkeypatch):
    from rlsbl.commands import watch

    monkeypatch.chdir(tmp_path)
    _archive(tmp_path / ".rlsbl" / "releases", "1.2.0")
    (tmp_path / ".rlsbl" / "config.json").write_text('{"publish_mode": "ci"}\n')
    with patch("rlsbl.commands.watch.run_gh", side_effect=_gh):
        url = watch._release_url("o/r")
    assert url == "https://github.com/o/r/releases/tag/v1.2.0"
