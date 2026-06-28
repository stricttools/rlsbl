"""Release state file: persistence for idempotent release flow.

Tracks which mutating steps have completed so that a failed release can
be resumed without re-executing already-done work.  The state file lives
at `.rlsbl/releases/in-progress.json` and is written at the start of the
mutating phase, deleted on success, and left in place on failure.
"""

import json
import os
import tempfile


STATE_FILENAME = "in-progress.json"


def get_state_path(project_dir: str) -> str:
    """Return the path to the release state file."""
    return os.path.join(project_dir, ".rlsbl", "releases", STATE_FILENAME)


def save_release_state(state_path: str, state_dict: dict) -> None:
    """Atomically write the release state dict to disk (tmp + os.replace)."""
    parent = os.path.dirname(state_path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".in-progress.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state_dict, f, indent=2)
            f.write("\n")
        os.replace(tmp, state_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_release_state(state_path: str) -> dict | None:
    """Read and parse the release state file.  Returns None if missing."""
    if not os.path.isfile(state_path):
        return None
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_step(state_path: str, step_name: str) -> None:
    """Record a completed step: load, append to completed_steps, save."""
    state = load_release_state(state_path)
    if state is None:
        state = {"completed_steps": []}
    steps = state.setdefault("completed_steps", [])
    if step_name not in steps:
        steps.append(step_name)
    save_release_state(state_path, state)


def clear_release_state(state_path: str) -> None:
    """Delete the state file and its parent dir if empty (no-op if already absent)."""
    try:
        os.unlink(state_path)
    except FileNotFoundError:
        pass
    # Remove parent directory if it's now empty (best-effort).
    # The state file may have created .rlsbl/releases/ which would be
    # left as an untracked directory after git reset --hard.
    try:
        parent = os.path.dirname(state_path)
        if parent and os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass
