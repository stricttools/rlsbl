"""A config naming a release target rlsbl does not support is refused.

rlsbl supports the targets registered in ``rlsbl.targets.TARGETS``. A
configured name outside that registry -- one of the targets rlsbl no longer
supports, or a misspelling -- is a hard error naming every supported target,
never a warning that skips the target and releases the rest.
"""

import json
import subprocess
import sys

import pytest

from rlsbl.errors import ConfigError
from rlsbl.targets import TARGETS, detect_targets, read_releasable_targets

UNSUPPORTED = [
    "dart", "deno", "docker", "flutter", "hex", "maven", "native-android",
    "native-ios", "pgdesign", "plain", "swift", "swift-apple", "zig",
]
SUPPORTED_LIST = ", ".join(sorted(TARGETS))


def _project(tmp_path, targets):
    (tmp_path / ".rlsbl").mkdir()
    (tmp_path / ".rlsbl" / "config.json").write_text(
        json.dumps({"targets": targets, "publish_mode": "none"})
    )
    return tmp_path


def test_the_supported_targets_are_go_npm_pypi_and_spec():
    assert sorted(TARGETS) == ["go", "npm", "pypi", "spec"]


@pytest.mark.parametrize("name", UNSUPPORTED)
def test_a_project_config_naming_an_unsupported_target_is_refused(tmp_path, name):
    project = _project(tmp_path, ["go", name])
    with pytest.raises(ConfigError) as exc_info:
        detect_targets(str(project))
    message = str(exc_info.value)
    assert f"names target '{name}'" in message
    assert f"Supported targets: {SUPPORTED_LIST}." in message


def test_a_record_entry_naming_an_unsupported_target_is_refused(tmp_path):
    project = _project(tmp_path, [{"name": "zig", "path": "."}])
    with pytest.raises(ConfigError, match="names target 'zig'"):
        detect_targets(str(project))


@pytest.mark.parametrize("entry", ["docker", {"name": "docker", "path": "img"}])
def test_a_releasable_config_naming_an_unsupported_target_is_refused(tmp_path, entry):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"targets": [entry]}))
    with pytest.raises(ConfigError) as exc_info:
        read_releasable_targets(str(config))
    message = str(exc_info.value)
    assert str(config) in message
    assert f"Supported targets: {SUPPORTED_LIST}." in message


def test_the_cli_refuses_the_config_and_names_the_supported_targets(tmp_path):
    project = _project(tmp_path, ["pgdesign"])
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)

    result = subprocess.run(
        [sys.executable, "-P", "-m", "rlsbl", "targets"],
        cwd=project, capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "names target 'pgdesign'" in result.stderr
    assert f"Supported targets: {SUPPORTED_LIST}." in result.stderr
    assert "Traceback" not in result.stderr


def test_removing_the_unsupported_target_clears_the_refusal(tmp_path):
    project = _project(tmp_path, ["go", "zig"])
    with pytest.raises(ConfigError):
        detect_targets(str(project))

    (project / ".rlsbl" / "config.json").write_text(
        json.dumps({"targets": ["go"], "publish_mode": "none"})
    )

    assert [entry.name for entry in detect_targets(str(project))] == ["go"]
