package targets

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestPrivatePathsAreMatchedByTheirOutermostRule(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct{ rel, rule, dir string }{
		{"todo/plan.md", "/todo/", "todo"},
		{"src/todo/item.py", "", ""},
		{".strictmetadata/releases/portal/v1.0.0.toml", ".strictmetadata/", ".strictmetadata"},
		{"pkg/.claude/settings.json", ".claude/", "pkg/.claude"},
		{"CLAUDE.md", "CLAUDE.md", ""},
		{"docs/AGENTS.md", "AGENTS.md", ""},
		{".env.production", ".env*", ""},
		{"config/notes.local-only/a.txt", "*.local-only", "config/notes.local-only"},
		{"testdata/.env", "", ""},
		{"pkg/testdata/CLAUDE.md", "", ""},
		{"experiments/probe.go", "/experiments/", "experiments"},
		{"main.go", "", ""},
	}
	for _, c := range cases {
		rule, dir, found := PrivateMatch(c.rel)
		if found != (c.rule != "") || rule != c.rule || dir != c.dir {
			t.Errorf("PrivateMatch(%q) = %q, %q, %v", c.rel, rule, dir, found)
		}
	}
	got := PrivatePathsIn([]string{"a.go", "CLAUDE.md", "todo/x"})
	if len(got) != 2 || got[0].Rel != "CLAUDE.md" || got[1].Rule != "/todo/" {
		t.Fatalf("PrivatePathsIn = %+v", got)
	}
}

func TestEveryRuleIsAnExcludeEntry(t *testing.T) {
	hygiene.Isolate(t)
	entries := ExcludeEntries()
	for _, rel := range []string{"todo/a", ".selfdoc/b", "AGENTS.md", ".envrc", "x.local-only"} {
		rule, _, found := PrivateMatch(rel)
		if !found || !contains(entries, rule) {
			t.Errorf("%s matched the rule %q, which ExcludeEntries does not hold", rel, rule)
		}
	}
	if !strings.HasPrefix(NpmignoreBlock(), "/todo/\n/experiments/\n") {
		t.Errorf("NpmignoreBlock = %q", NpmignoreBlock())
	}
}

func TestEachBuildBackendGetsItsOwnFix(t *testing.T) {
	hygiene.Isolate(t)
	cases := []struct{ backend, part, want string }{
		{"hatchling.build", "sdist", "add \"/todo/\" to exclude under [tool.hatch.build.targets.sdist] in pyproject.toml"},
		{"uv_build", "wheel", "add \"/todo/\" to wheel-exclude under [tool.uv.build-backend] in pyproject.toml"},
		{"setuptools.build_meta", "sdist", "add \"prune todo\" to MANIFEST.in"},
		{"setuptools.build_meta", "wheel", "leave todo out of the packages and package data under [tool.setuptools] in pyproject.toml"},
		{"", "sdist", "exclude /todo/ from the sdist in the configuration of the default build backend"},
	}
	for _, c := range cases {
		if got := PythonFix(c.backend, c.part, "todo/plan.md", "/todo/", "todo"); got != c.want {
			t.Errorf("%s %s: %s", c.backend, c.part, got)
		}
	}
}
