package release_test

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/release"
)

func TestTheStepTableHoldsTheReleasesStepsInOrder(t *testing.T) {
	hygiene.Isolate(t)
	want := "version-bumped,committed,candidate-pushed,ci-verified,changelog-finalized,release-archived,tagged,pushed,github-release-created,pipelines-published,deployed,post-release-hooks-run"
	if got := strings.Join(release.StepNames(), ","); got != want {
		t.Errorf("steps: %s", got)
	}
	fatal := release.FatalSteps()
	for _, name := range release.StepNames() {
		if fatal[name] == (name == release.StepPostReleaseHooksRun) {
			t.Errorf("%s: fatal %v", name, fatal[name])
		}
	}
	step, ok := release.StepNamed(release.StepVersionBumped)
	var entries []string
	for _, e := range step.Entries {
		entries = append(entries, string(e))
	}
	if !ok || strings.Join(entries, ",") != "write-releasable-version,write-target-versions,write-member-versions,bump-selfdoc,ensure-keyword,sync-lockfiles,write-scaffold-state,clean-artifacts,build,secret-scan,packed-artifact-contents,guard-unexpected-files" {
		t.Errorf("the version-bumped entries: %v", entries)
	}
	if _, ok := release.StepNamed("snapshot-regenerated"); ok {
		t.Error("a dropped step is in the table")
	}
}
