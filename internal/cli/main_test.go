package cli

import (
	"os"
	"testing"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// TestMain hands the process to the fake gh when the test binary was
// started as gh (testsupport.FakeGH), and runs the tests otherwise.
func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}
