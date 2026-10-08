package cli

import (
	"os"
	"testing"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// TestMain runs the tests through testsupport.RunTests, which builds the
// fake gh they use.
func TestMain(m *testing.M) {
	os.Exit(testsupport.RunTests(m))
}
