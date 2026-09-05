"""The ``[[releasables]]`` reader and writer both hold the section to the model.

``save_workspace`` reconciles ``[[releasables]]`` from a list of
:class:`~rlsbl.workspace_types.Releasable` instances, which carry only the keys
the model knows. A key the model does NOT know is refused at load, so there is
none to carry across a rewrite -- writing one back would produce a file rlsbl
then refuses to read. What the rewrite must preserve is every entry the command
never named, and the formatting around them.
"""

import json
import os

import pytest

from rlsbl.commands.monorepo import _cmd_add, _cmd_init
from rlsbl.errors import WorkspaceError
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    Releasable,
    load_releasables,
    load_workspace,
    save_workspace,
)


def _npm_project(base_path, subdir):
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": "test-" + os.path.basename(subdir), "version": "0.1.0"}, f)
    return proj_dir


def _ws_path(root):
    return os.path.join(str(root), WORKSPACE_DIR, WORKSPACE_FILE)


def _workspace_text(root):
    with open(_ws_path(root), encoding="utf-8") as f:
        return f.read()


def _write_workspace_text(root, text):
    with open(_ws_path(root), "w", encoding="utf-8") as f:
        f.write(text)


class TestTheRewriteHoldsTheSectionToTheModel:
    def test_add_creating_a_releasable_keeps_another_entry_intact(self, mock_git_repo):
        """Creating one entry never edits a sibling entry the command never named."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _write_workspace_text(mock_git_repo, _workspace_text(mock_git_repo).replace(
            "releasables = []",
            '[[releasables]]\n'
            'name = "legacy"\n'
            'tag_format = "v{version}"\n',
        ))

        _npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "pkg-a"}, project_root=".")

        text = _workspace_text(mock_git_repo)
        assert 'name = "legacy"' in text
        assert 'tag_format = "v{version}"' in text

    def test_save_workspace_preserves_the_entries_it_was_given(self, mock_git_repo):
        """Appending one entry leaves the existing entry byte-intact."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _write_workspace_text(mock_git_repo, _workspace_text(mock_git_repo).replace(
            "releasables = []",
            '[[releasables]]\n'
            'name = "core"\n'
            'tag_format = "v{version}"\n',
        ))

        root = str(mock_git_repo)
        projects = load_workspace(root)
        releasables = load_releasables(root, projects)
        save_workspace(
            root, projects,
            releasables=list(releasables) + [
                Releasable(name="extra", tag_format="{name}@v{version}"),
            ],
        )

        text = _workspace_text(mock_git_repo)
        assert 'name = "core"' in text
        assert 'tag_format = "v{version}"' in text
        assert 'name = "extra"' in text

    def test_an_unknown_key_on_a_releasable_table_is_refused_at_load(
        self, mock_git_repo
    ):
        """The reason nothing carries unknown releasable keys across a rewrite."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _write_workspace_text(mock_git_repo, _workspace_text(mock_git_repo).replace(
            "releasables = []",
            '[[releasables]]\n'
            'name = "core"\n'
            'future_key = ["a", "b"]\n',
        ))

        with pytest.raises(WorkspaceError, match="future_key"):
            load_workspace(str(mock_git_repo))

    def test_an_unknown_key_on_a_freshly_appended_entry_is_not_invented(self, mock_git_repo):
        """A newcomer carries exactly the model's keys and nothing else."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        root = str(mock_git_repo)
        projects = load_workspace(root)
        save_workspace(
            root, projects,
            releasables=[Releasable(name="core", tag_format="v{version}")],
        )
        text = _workspace_text(mock_git_repo)
        assert "future_key" not in text
        assert 'tag_format = "v{version}"' in text


class TestAppendedTableSpacing:
    def test_an_appended_releasable_is_separated_from_the_projects_section(self, mock_git_repo):
        """An appended [[releasables]] table does not butt against [[projects]]."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _write_workspace_text(mock_git_repo, _workspace_text(mock_git_repo).replace(
            "releasables = []", '[[releasables]]\nname = "legacy"',
        ))

        _npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "pkg-a"}, project_root=".")

        text = _workspace_text(mock_git_repo)
        idx = text.index("[[projects]]")
        assert text[:idx].endswith("\n\n"), repr(text)
