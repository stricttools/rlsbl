"""Every npm publish template installs dependencies before publishing.

`npm publish` (and the pnpm/yarn equivalents) runs the package's `prepack`
lifecycle script. For a TypeScript package that compiles in prepack, the build
toolchain lives in devDependencies -- absent from a bare CI checkout. Two
separate ecosystem projects hit the same `error TS2688: Cannot find type
definition file for 'node'` at publish time before the templates carried an
install step.

The assertion granularity is render-level and ordering-sensitive: an install
step must EXIST and must PRECEDE the publish step. The monorepo publish router
inlines each project's publish job verbatim, so it inherits the step -- proven
here rather than assumed.
"""

import os
import textwrap
from unittest.mock import patch

import pytest
from ruamel.yaml import YAML

from rlsbl.commands.monorepo.publish_inline import generate_inline_publish_router
from rlsbl.targets.npm import NpmTarget

NPM_TEMPLATE_DIR = NpmTarget().template_dir()

# template file -> (install command, publish step line). The publish marker
# carries the "- run: " prefix so it matches the STEP, never a prose mention
# of the same command inside a comment.
NPM_PUBLISH_TEMPLATES = {
    "publish.yml.tpl": ("npm ci", "- run: npm publish"),
    "publish-launcher.yml.tpl": ("npm ci", "- run: npm publish"),
    "publish-pnpm.yml.tpl": (
        "pnpm install --frozen-lockfile", "- run: pnpm publish",
    ),
    "publish-yarn.yml.tpl": (
        "yarn install --immutable", "- run: yarn npm publish",
    ),
}


def _read_template(name):
    with open(os.path.join(NPM_TEMPLATE_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


class TestNpmPublishTemplatesInstallDeps:
    """All four npm publish templates, not just the plain-npm one."""

    def test_every_npm_template_is_covered(self):
        """A new npm publish template must be added to this matrix."""
        on_disk = {
            name for name in os.listdir(NPM_TEMPLATE_DIR)
            if name.startswith("publish") and name.endswith(".yml.tpl")
        }
        assert on_disk == set(NPM_PUBLISH_TEMPLATES), on_disk

    @pytest.mark.parametrize("name", sorted(NPM_PUBLISH_TEMPLATES))
    def test_install_step_exists(self, name):
        install, _publish = NPM_PUBLISH_TEMPLATES[name]
        content = _read_template(name)
        assert f"run: {install}" in content

    @pytest.mark.parametrize("name", sorted(NPM_PUBLISH_TEMPLATES))
    def test_install_precedes_publish(self, name):
        install, publish = NPM_PUBLISH_TEMPLATES[name]
        content = _read_template(name)
        assert content.index(f"run: {install}") < content.index(publish)

    @pytest.mark.parametrize("name", sorted(NPM_PUBLISH_TEMPLATES))
    def test_install_is_not_production_only(self, name):
        """`--omit=dev` / `--production` would reintroduce the exact bug."""
        content = _read_template(name)
        install_line = next(
            line for line in content.splitlines()
            if line.strip().startswith(f"run: {NPM_PUBLISH_TEMPLATES[name][0]}")
        )
        assert "--omit=dev" not in install_line
        assert "--production" not in install_line


NPM_PUBLISH_WF = textwrap.dedent("""\
    name: Publish

    on:
      release:
        types: [published]

    jobs:
      publish:
        runs-on: ubuntu-latest
        permissions:
          contents: read
          id-token: write
        steps:
          - uses: actions/checkout@v6
          - uses: actions/setup-node@v4
          - name: Install dependencies
            run: npm ci
          - run: npm publish --access public
            env:
              NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
""")


class TestRouterInheritsInstallStep:
    """`rlsbl monorepo sync` inlines the job, so the fix propagates on re-sync."""

    def test_inlined_job_keeps_install_before_publish(self, tmp_path):
        root = str(tmp_path)
        wf_dir = os.path.join(root, "packages", "mylib", ".github", "workflows")
        os.makedirs(wf_dir)
        with open(os.path.join(wf_dir, "publish.yml"), "w") as f:
            f.write(NPM_PUBLISH_WF)

        projects = [{"name": "mylib", "path": "packages/mylib"}]
        with patch(
            "rlsbl.commands.monorepo.publish_inline._get_monorepo_tag_prefix",
            return_value="mylib@v",
        ):
            router = generate_inline_publish_router(projects, root)

        parsed = YAML(typ="safe").load(router)
        steps = parsed["jobs"]["mylib-publish"]["steps"]
        runs = [s.get("run", "") for s in steps]
        install_idx = next(i for i, r in enumerate(runs) if r.strip() == "npm ci")
        publish_idx = next(i for i, r in enumerate(runs) if "npm publish" in r)
        assert install_idx < publish_idx
