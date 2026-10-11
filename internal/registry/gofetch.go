package registry

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// goFetchTimeout bounds the request for a version the proxy has not fetched
// yet, which it answers only once it has fetched the version from its origin.
const goFetchTimeout = 2 * time.Minute

// RequestGoVersion requests one version of a Go module from the Go module
// proxy (its info document), which makes the proxy fetch it, and returns nil
// only when the proxy returns that version. The request is a recorded effect,
// never a declared read: the proxy records a version the first time anyone
// asks for it, so the caller makes it only for a version whose tag is pushed,
// and a dry run records it instead of sending it.
func RequestGoVersion(e *strictcli.Effects, module, version string) error {
	url := goInfoURL(module, version)
	resp, err := e.HTTP("GET", url, strictcli.Check(false), strictcli.Timeout(goFetchTimeout))
	if err != nil {
		return fmt.Errorf("GET %s: %w", url, err)
	}
	if resp.Status() != 200 {
		return fmt.Errorf("GET %s answered HTTP %d: %s", url, resp.Status(), strings.TrimSpace(string(resp.Body())))
	}
	var info struct{ Version string }
	if err := json.Unmarshal(resp.Body(), &info); err != nil {
		return fmt.Errorf("GET %s answered a document that does not parse: %w", url, err)
	}
	if info.Version != version {
		return fmt.Errorf("GET %s answered version %q", url, info.Version)
	}
	return nil
}
