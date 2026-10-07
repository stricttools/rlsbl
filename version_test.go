package rlsbl

import (
	"os"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/semver"
)

// The binary's version is the VERSION file's, and it is a release version.
func TestVersionIsTheVersionFile(t *testing.T) {
	hygiene.Isolate(t)
	data, err := os.ReadFile("VERSION")
	if err != nil {
		t.Fatal(err)
	}
	if Version != strings.TrimSpace(string(data)) {
		t.Fatalf("Version = %q, VERSION holds %q", Version, data)
	}
	if _, err := semver.Parse(Version); err != nil {
		t.Fatal(err)
	}
}
