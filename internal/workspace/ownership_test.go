package workspace

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestToolOwnedPaths(t *testing.T) {
	hygiene.Isolate(t)
	for _, p := range []string{
		".strictmetadata/changelog/portal/unreleased.jsonl",
		".strictmetadata/releases/portal/v0.1.0.toml",
		".strictmetadata/releases/portal/version",
		".strictmetadata/batch-releases/unreleased.toml",
		".strictmetadata/transitions/transitions.jsonl",
		".strictmetadata/history-rewrites/20260101T000000Z.toml",
		".strictmetadata/retired-release-histories/old/releases/version",
		".strictmetadata/.scaffold-state/scaffold-state.toml",
		".strictmetadata/.scaffold-bases/.github/workflows/ci.yml",
		".strictmetadata/.changelog-validation/portal.toml",
		".strictmetadata/.release-state/lock",
		".strictmetadata/go.mod",
		".github/workflows/ci-router.yml",
		"CHANGELOG.md",
		"pkg/CHANGELOG.md",
		".rlsbl/changes/unreleased.jsonl",
		"pkg/.rlsbl/releases/v1.toml",
		".rlsbl/version",
		".rlsbl-monorepo/workspace.toml",
	} {
		if !IsToolOwned(p) {
			t.Errorf("%s is not tool-owned", p)
		}
	}
	for _, p := range []string{
		"README.md",
		".strictmetadata/releasables/releasables.toml",
		".strictmetadata/test-runner/test-runner.toml",
		".strictmetadata/release-hooks/portal/pre-release.sh",
		".strictmetadata/lifecycle-and-license/lifecycle-and-license.toml",
		".strictmetadata/options/rlsbl.toml",
		"pkg/.strictmetadata/changelog/x.jsonl",
		"pkg/.github/workflows/ci-router.yml",
		"pkg/.rlsbl-monorepo/workspace.toml",
		".rlsbl/config.json",
		".github/workflows/publish.yml",
	} {
		if IsToolOwned(p) {
			t.Errorf("%s is tool-owned by %s", p, ToolOwnedRule(p))
		}
	}
	if rule := ToolOwnedRule("./.strictmetadata/changelog/x/"); rule != ".strictmetadata/changelog/**" {
		t.Errorf("ToolOwnedRule = %q", rule)
	}
}

func TestEveryFileHasOneOwnerTheMostSpecificMember(t *testing.T) {
	hygiene.Isolate(t)
	w := newWorkspace(t, t.TempDir(), nested)
	for _, c := range [][2]string{
		{"README.md", "root"},
		{"draw/main.go", "draw"},
		{"draw/cmd/main.go", "cmd"},
		{"draw/cmdx/main.go", "draw"},
		{"kernel/a.c", "kernel"},
		{"kernelx/a.c", "root"},
	} {
		m, ok := w.OwnerOf(c[0])
		if !ok || m.Name != c[1] {
			t.Errorf("OwnerOf(%s) = %q, want %q", c[0], m.Name, c[1])
		}
	}
	if m, ok := w.OwnerOf("draw/CHANGELOG.md"); ok {
		t.Errorf("a tool-owned path is owned by %s", m.Name)
	}
	affected := w.AffectedMembers([]string{"kernel/a.c", "draw/cmd/x", "README.md", "CHANGELOG.md"})
	var names []string
	for _, m := range affected {
		names = append(names, m.Name)
	}
	if got := strings.Join(names, " "); got != "root cmd kernel" {
		t.Errorf("AffectedMembers = %s", got)
	}
}

func TestAReleasableScopeClaimsItsStateAndNoOtherReleasables(t *testing.T) {
	hygiene.Isolate(t)
	w := newWorkspace(t, t.TempDir(), nested)
	draw := w.ScopeOfReleasable("draw")
	for _, p := range []string{
		"draw/main.go",
		".strictmetadata/changelog/draw/unreleased.jsonl",
		".strictmetadata/releases/draw",
		".strictmetadata/.changelog-validation/draw.toml",
		".rlsbl-monorepo/releasables/draw/version",
	} {
		if !draw.Claims(p) {
			t.Errorf("the draw scope does not claim %s", p)
		}
	}
	for _, p := range []string{
		"draw/cmd/main.go",
		".strictmetadata/changelog/cmd/unreleased.jsonl",
		".strictmetadata/changelog/drawing/unreleased.jsonl",
		"CHANGELOG.md",
		"README.md",
	} {
		if draw.Claims(p) {
			t.Errorf("the draw scope claims %s", p)
		}
	}
	member := w.ScopeOfMember(w.Members()[1])
	if member.Claims(".strictmetadata/changelog/draw/unreleased.jsonl") {
		t.Error("a member's scope claims its releasable's state")
	}
	if got := draw.Describe(); !strings.HasPrefix(got, "draw (and ") || !strings.Contains(got, ".strictmetadata/changelog/draw") {
		t.Errorf("Describe = %q", got)
	}
	if got := member.Describe(); got != "draw" {
		t.Errorf("Describe = %q", got)
	}
}

func TestStateDirReleasableInvertsStateDirs(t *testing.T) {
	hygiene.Isolate(t)
	for _, p := range []string{
		".strictmetadata/changelog/portal",
		".strictmetadata/changelog/portal/unreleased.jsonl",
		".strictmetadata/releases/portal/version",
		".strictmetadata/.changelog-validation/portal.toml",
		".strictmetadata/.release-state/portal/in-progress.toml",
		".rlsbl-monorepo/releasables/portal/config.json",
	} {
		if name, ok := StateDirReleasable(p); !ok || name != "portal" {
			t.Errorf("StateDirReleasable(%s) = %q, %t", p, name, ok)
		}
	}
	for _, p := range []string{
		"README.md",
		".strictmetadata/changelog",
		".strictmetadata/.release-state/lock",
		".strictmetadata/.release-state/batch-plan.toml",
	} {
		if name, ok := StateDirReleasable(p); ok {
			t.Errorf("StateDirReleasable(%s) = %q", p, name)
		}
	}
}
