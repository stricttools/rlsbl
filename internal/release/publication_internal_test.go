package release

import (
	"testing"
	"time"

	"github.com/stricttools/testisolation/go/hygiene"
)

// npm's registry serves a package document from its CDN for up to five
// minutes (cache-control: public, max-age=300), and the publish workflow
// asks for the document before publishing, so the copy cached then can be
// served for five minutes after the publish. The wait for the listing
// outlasts that.
func TestThePublicationWaitOutlastsNpmsCachedPackageDocument(t *testing.T) {
	hygiene.Isolate(t)
	var elapsed time.Duration
	listedAfter := 5*time.Minute + 5*time.Second
	missing, err := awaitPublication(func(d time.Duration) { elapsed += d }, func() ([]string, error) {
		if elapsed < listedAfter {
			return []string{"portal on npm"}, nil
		}
		return nil, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(missing) != 0 {
		t.Fatalf("gave up after %s while npm's cached document was still being served: %v", elapsed, missing)
	}
}
