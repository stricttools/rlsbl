// Command gh is the fake gh testsupport.FakeGH puts on PATH.
package main

import (
	"os"

	"github.com/stricttools/rlsbl/internal/testsupport/fakegh"
)

func main() {
	os.Exit(fakegh.Serve())
}
