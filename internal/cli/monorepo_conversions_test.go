package cli

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestExtractAndAbsorbAreConsequential(t *testing.T) {
	hygiene.Isolate(t)
	group, ok := appWith(t, testsupport.NewFakeHTTP(t)).Groups()["monorepo"]
	if !ok {
		t.Fatal("no monorepo group is registered")
	}
	for _, name := range []string{"extract", "absorb"} {
		cmd, ok := group.Commands[name]
		if !ok || cmd.Effect != strictcli.EffectMutating || !cmd.Consequential {
			t.Errorf("monorepo %s: registered %v: %+v", name, ok, cmd)
		}
	}
}

func TestMonorepoExtractRefusesAnUndeclaredReleasableThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	monorepoFixture(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"monorepo", "extract", "widgets", filepath.Join(t.TempDir(), "widgets"), "--dry-run", "--approve-consequential"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "the declared releasables are widget, gadget") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestMonorepoExtractPreviewsThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	if _, err := exec.LookPath("git-filter-repo"); err != nil {
		t.Skip("git-filter-repo is not installed")
	}
	testsupport.FakeSaferm(t)
	monorepoFixture(t)
	target := filepath.Join(t.TempDir(), "gadget")
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"monorepo", "extract", "gadget", target, "--dry-run", "--approve-consequential"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "releasable: extract-to-standalone") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if _, err := os.Lstat(target); !os.IsNotExist(err) {
		t.Errorf("the preview created %s", target)
	}
}

func TestMonorepoAbsorbRefusesAMissingSourceThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	monorepoFixture(t)
	missing := filepath.Join(t.TempDir(), "nothing")
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"monorepo", "absorb", missing, "packages/gizmo", "--tag-format", "{name}@v{version}", "--dry-run", "--approve-consequential"})
	if r.ExitCode != 1 || !strings.Contains(r.Stderr, "does not exist") {
		t.Fatalf("exit %d\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}
