package cli

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestTargetsThroughTheApplication(t *testing.T) {
	hygiene.Isolate(t)
	releaseCommandsProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	r := app.Test([]string{"targets"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d: %s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	for _, want := range []string{"Targets of the member root (detected from its manifests):", "Target  In this member  Version file", "npm     yes             package.json", "go      no              VERSION"} {
		if !strings.Contains(r.Stdout, want) {
			t.Errorf("%q is not in:\n%s", want, r.Stdout)
		}
	}
	r = app.Test([]string{"targets", "--json"})
	if r.ExitCode != 0 || len(jsonPayload(t, r)["targets"].([]any)) != 3 {
		t.Fatalf("exit %d: %s", r.ExitCode, r.Stdout)
	}
}
