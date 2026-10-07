package registry

import (
	"strings"
	"testing"
	"time"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

const gadgetDocument = `{"info":{"version":"0.3.0"},"releases":{
	"0.2.0":[{"yanked":true},{"yanked":true}],
	"0.3.0":[{"yanked":false},{"yanked":true}],
	"0.4.0":[]}}`

func TestPypiReleaseReadsTheYankedStateFromTheProjectDocument(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t,
		get("https://pypi.org/pypi/gadget/json", 200, gadgetDocument),
		get("https://pypi.org/pypi/missing/json", 404, `{"message":"Not Found"}`),
	)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		cases := []struct {
			version            string
			listed, yanked     bool
			files, yankedFiles int
		}{
			{"0.2.0", true, true, 2, 2},
			{"0.3.0", true, false, 2, 1},
			{"0.4.0", true, false, 0, 0},
			{"0.5.0", false, false, 0, 0},
		}
		for _, tc := range cases {
			r, found, err := c.PypiRelease("gadget", tc.version)
			if err != nil || !found {
				t.Fatalf("%s: found %v, err %v", tc.version, found, err)
			}
			if r.Listed != tc.listed || r.IsYanked() != tc.yanked || r.Files != tc.files || r.Yanked != tc.yankedFiles {
				t.Fatalf("%s: %+v (yanked %v)", tc.version, r, r.IsYanked())
			}
		}
		if _, found, err := c.PypiRelease("missing", "0.1.0"); err != nil || found {
			t.Fatalf("a missing project: found %v, err %v", found, err)
		}
		return nil
	})
	for _, u := range fake.URLs() {
		if strings.Contains(u, "0.") {
			t.Fatalf("a request named a version: %s", u)
		}
		if !packageLevel[3].MatchString(u) {
			t.Fatalf("%s is not the project document", u)
		}
	}
}

func TestPypiReleaseRefusesAFileWithoutItsYankedState(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t, get("https://pypi.org/pypi/gadget/json", 200, `{"releases":{"0.2.0":[{}]}}`))
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		_, _, err := c.PypiRelease("gadget", "0.2.0")
		if err == nil || !strings.Contains(err.Error(), "without its yanked state") {
			t.Fatalf("err %v", err)
		}
		return nil
	})
}
