"""The primitives both repository conversions share.

NEITHER conversion is here any more. Both are reconcilers on the shared
observe/preview/apply skeleton and both live beside this module:
``rlsbl monorepo extract`` in :mod:`rlsbl.commands.monorepo.extract_cmd`, and
``rlsbl monorepo absorb`` in :mod:`rlsbl.commands.monorepo.absorb_cmd`.

What stays is what both directions need: the shared error type, the
git-filter-repo dependency check, the git and filter-repo runners with the
timeouts a whole-history rewrite needs, the clone identity fix, and the
dangling-changelog-entry pruning a rewrite leaves behind.
"""

import dataclasses
import os
import shutil
import subprocess
import sys

from ...changelog.files import writable_jsonl
from ...changelog.schema import parse_jsonl, serialize_entry
from ...errors import RlsblError
from ... import effects


class ExtractError(RlsblError):
    """Error during extract or absorb operations."""


#: Seconds a single git invocation in a conversion may take. Generous next to
#: the 120s the shared runner uses, because a conversion's git calls include
#: whole-repository clones and merges rather than status reads -- and bounded,
#: because a conversion that hangs forever is worse than one that fails.
GIT_TIMEOUT = 600

#: Seconds a git-filter-repo run may take. Larger again: rewriting every commit
#: of a long history is the slowest thing either conversion does.
FILTER_REPO_TIMEOUT = 3600


def require_filter_repo():
    """Raise if git-filter-repo is not installed.

    Checks that the ``git-filter-repo`` command is available on PATH.
    Raises ExtractError with install instructions if missing.
    """
    path = shutil.which("git-filter-repo")
    if path is None:
        raise ExtractError(
            "git-filter-repo is not installed. "
            "Install it with: pip install git-filter-repo\n"
            "Or see: https://github.com/newren/git-filter-repo#how-do-i-install-it"
        )
    return path


def _run_git(cwd, *args, timeout=GIT_TIMEOUT):
    """Run a git command and return stdout. Raises subprocess.CalledProcessError on failure."""
    result = effects.run(
        ["git"] + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _run_filter_repo(cwd, *args):
    """Run ``git-filter-repo`` in ``cwd``, wrapping failures in ExtractError.

    Raw ``subprocess.CalledProcessError`` from a filter-repo run is an opaque
    stack trace to the caller; wrap it so the extract flow reports a clean,
    actionable error including filter-repo's own stderr.
    """
    try:
        effects.run(
            ["git-filter-repo", *args],
            cwd=str(cwd), check=True, capture_output=True, text=True,
            timeout=FILTER_REPO_TIMEOUT,
        )
    except subprocess.CalledProcessError as exc:
        raise ExtractError(
            f"git-filter-repo failed (args: {' '.join(args)}):\n"
            f"{exc.stderr or exc.stdout or exc}"
        ) from exc


def _ensure_git_identity(clone_path, source_path):
    """Copy the source repo's committer identity into a fresh clone.

    ``git clone`` does not carry over the source's *local* ``user.name`` /
    ``user.email``, so a commit inside the clone can fail with "please tell me
    who you are" in environments without a global identity. We read the
    source's effective identity (local or global) and set it locally in the
    clone. If the source has none configured, the clone inherits whatever
    global identity exists (unchanged).
    """
    for key in ("user.name", "user.email"):
        try:
            val = _run_git(source_path, "config", "--get", key)
        except subprocess.CalledProcessError:
            continue
        if val:
            _run_git(clone_path, "config", key, val)


def _commit_resolves(repo, commit_hash):
    """Whether ``commit_hash`` resolves to an existing commit object in ``repo``."""
    result = effects.run(
        ["git", "cat-file", "-e", commit_hash + "^{commit}"],
        cwd=str(repo), capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    return result.returncode == 0


def _prune_dangling_entries(changes_dir, repo_root):
    """Drop changelog entries whose commits no longer resolve after a rewrite.

    Runs AFTER :func:`remap_jsonl_hashes` has mapped every survivable hash to
    its post-rewrite SHA. Any commit that still fails to resolve in
    ``repo_root`` was pruned by the filter (or was never mappable):

    - An entry with at least one surviving commit is kept, narrowed to just the
      resolving hashes (a partial survival, logged).
    - An entry whose EVERY commit fails to resolve is DROPPED entirely, with a
      loud log line -- never left dangling with a null/stale hash.

    Returns ``{jsonl filename: entries dropped}``, naming only the files that
    LOST entries. A dropped entry changes what the version's generated markdown
    should say, so the caller regenerates exactly those files' ``.md``; a
    narrowed entry does not (the markdown carries descriptions, not hashes).
    """
    if not os.path.isdir(changes_dir):
        return {}
    dropped = {}
    for name in sorted(os.listdir(changes_dir)):
        if not name.endswith(".jsonl"):
            continue
        filepath = os.path.join(changes_dir, name)
        entries = parse_jsonl(filepath)
        new_entries = []
        changed = False
        for entry in entries:
            surviving = [h for h in entry.commits if _commit_resolves(repo_root, h)]
            if not surviving:
                changed = True
                dropped[name] = dropped.get(name, 0) + 1
                desc = entry.description or "(non-user-facing)"
                print(
                    f"note: dropping changelog entry '{desc}' from {name} -- "
                    f"all referenced commits were pruned by the extract "
                    f"rewrite",
                    file=sys.stderr,
                )
                continue
            if len(surviving) != len(entry.commits):
                changed = True
                print(
                    f"note: narrowing changelog entry in {name} to surviving "
                    f"commits ({len(surviving)}/{len(entry.commits)}) after "
                    f"extract rewrite",
                    file=sys.stderr,
                )
                new_entries.append(dataclasses.replace(entry, commits=surviving))
            else:
                new_entries.append(entry)
        if not changed:
            continue
        content = "".join(serialize_entry(e) + "\n" for e in new_entries)
        with writable_jsonl(filepath):
            # preserve_mode keeps the file the mode it already had: the
            # unreleased JSONL is an ordinary 644 working file, and pinning a
            # mode here made it 600 on the way through. A released file's lock
            # is writable_jsonl's business, and it relocks on exit regardless.
            effects.atomic_write_text(filepath, content, preserve_mode=True)
    return dropped
