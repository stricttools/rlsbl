package checks

import (
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// privateStub is the stub go.mod keeping a private directory out of a Go
// module zip.
const privateStub = "module private.invalid/rlsbl-private\n"

func TestAPrivatePathInTheModuleZipFailsUntilAStubModuleLeavesItOut(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "ci", map[string]string{
		".strictmetadata/go.mod": privateStub,
		"todo/plan.md":           "a plan\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "upload-private-paths")
	mustStatus(t, got, "fail")
	mustMention(t, got, "the Go module zip would ship todo/plan.md, a private path", "go.mod")
	// The fix the finding names: a stub go.mod in the private directory.
	r.CommitFile("todo/go.mod", privateStub, "Keep todo/ out of the module zip")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "upload-private-paths"), "pass")
}

func TestAProjectPublishingNothingHasNoUploadToList(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"todo/plan.md": "a plan\n"})
	got := runCheck(t, inputs(t, r.Dir), "upload-private-paths")
	mustStatus(t, got, "skip")
	mustMention(t, got, "no publishing member")
}
