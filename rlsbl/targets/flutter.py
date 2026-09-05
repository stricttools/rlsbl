"""Flutter release target that extends DartTarget, detecting Flutter projects by requiring the flutter: section in pubspec.yaml for version bumps."""

import os

from ruamel.yaml import YAML

from .dart import DartTarget


class FlutterTarget(DartTarget):
    """Release target for Flutter apps (pubspec.yaml with flutter: section)."""

    # Shares pubspec.yaml with dart; no unique detection files.
    detection_files = ()
    ecosystem = "Flutter"

    # A Flutter app IS Dart sources, so the inherited Dart import analysers
    # answer for it -- flutter is in scope for import analysis and cycle
    # detection exactly as dart is.
    #
    # It needs one thing plain Dart does not: an entry point. A Flutter APP has
    # no ``lib/<package>.dart`` barrel (nothing imports an app as a library)
    # and no ``bin/`` scripts (``flutter run`` is the runner), so both Dart
    # derivations come back empty -- and an empty entry-point set makes the
    # dead-module analysis return nothing, which reads as "clean" rather than
    # as "never ran". ``lib/main.dart`` is the entry point Flutter itself
    # defaults to, and naming it here is what makes the analysis actually run.
    #
    # Only that one file. Flavour entry points (``lib/main_dev.dart`` and the
    # like) are a per-project ``--target`` argument, not something Flutter
    # defines, so they are reported like any other unreferenced file and
    # suppressed per project if that is wrong for a given app.
    _convention_entry_points = ("lib/main.dart",)

    @property
    def name(self):
        return "flutter"

    def detect(self, dir_path):
        pubspec = os.path.join(dir_path, "pubspec.yaml")
        if not os.path.exists(pubspec):
            return False
        yaml = YAML(typ="safe")
        with open(pubspec, "r", encoding="utf-8") as f:
            data = yaml.load(f)
        if not isinstance(data, dict):
            return False
        return "flutter" in data

    def template_dir(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "flutter"
        )

    def template_mappings(self, ctx):
        return [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
        ]
