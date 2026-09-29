"""The publish router's gate checks every member of a multi-member releasable.

Every member of a releasable is tagged with the releasable's prefix, so a
router built one ``case`` branch per member repeated that prefix, and a shell
``case`` takes the first match: members after the first were never checked.
One branch per distinct prefix, whose regex covers every member's CI, is what
makes the gate demand all of them.
"""

import re

from rlsbl.publish_gate import build_router_gate_job


def _resolver(pairs):
    return build_router_gate_job(pairs)["steps"][0]["run"]


def test_one_case_branch_per_distinct_prefix():
    resolver = _resolver([
        ("foo@v", r"^(a\-foo\-ci) / "),
        ("foo@v", r"^(b\-foo\-ci) / "),
        ("foo@v", r"^(c\-foo\-ci) / "),
        ("bar@v", r"^(bar\-ci) / "),
    ])
    assert resolver.count('"foo@v"*)') == 1
    assert resolver.count('"bar@v"*)') == 1


def test_the_branch_regex_matches_every_members_checks():
    resolver = _resolver([
        ("foo@v", r"^(a\-foo\-ci) / "),
        ("foo@v", r"^(b\-foo\-ci) / "),
        ("foo@v", r"^(c\-foo\-ci) / "),
    ])
    regex = re.search(r"regex='([^']*)'", resolver).group(1)
    for check in ("a-foo-ci / test", "b-foo-ci / test", "c-foo-ci / lint"):
        assert re.search(regex, check), (regex, check)
    assert not re.search(regex, "other-ci / test")


def test_the_known_prefixes_are_listed_once():
    resolver = _resolver([
        ("foo@v", r"^(a) / "), ("foo@v", r"^(b) / "), ("bar@v", r"^(c) / "),
    ])
    known = re.search(r"known prefixes: ([^)]*)\)", resolver).group(1)
    assert sorted(known.split(", ")) == ["bar@v", "foo@v"]


def test_the_generated_router_for_a_three_member_releasable(tmp_path):
    from rlsbl.commands.monorepo.publish_inline import generate_inline_publish_router
    from rlsbl.workspace_types import Releasable

    projects = []
    for name in ("a-foo", "b-foo", "c-foo"):
        wf = tmp_path / name / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "publish.yml").write_text(
            "name: publish\non: release\njobs:\n  publish:\n"
            "    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        projects.append({
            "name": name, "path": name, "releasable": "foo",
            "_ci_files": [f"{name}-ci-npm.yml"],
        })
    router = generate_inline_publish_router(
        projects, str(tmp_path), releasables=[Releasable(name="foo")],
    )
    assert router.count('"foo@v"*)') == 1
    for name in ("a-foo", "b-foo", "c-foo"):
        assert re.escape(name) in router or name.replace("-", "\\-") in router
