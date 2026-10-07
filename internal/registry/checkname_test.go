package registry

import (
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

const npmSearchEmpty = `{"objects":[]}`

// npmFree answers 404 for every name in names.
func npmFree(names ...string) []testsupport.HTTPAnswer {
	var out []testsupport.HTTPAnswer
	for _, n := range names {
		out = append(out, get("https://registry.npmjs.org/"+n, 404, ""))
	}
	return out
}

func TestAnNpmNameThatIsRegisteredIsTaken(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t, get("https://registry.npmjs.org/portal", 200, `{"versions":{}}`))
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		r, err := c.CheckName(Npm, "portal", 0)
		if err != nil || r.Status != StatusTaken || r.Reason != "registered" || r.ExitCode() != 1 || !slices.Equal(r.Checked, []string{"npm"}) {
			t.Errorf("%+v %v", r, err)
		}
		return nil
	})
}

func TestAnNpmNameWhoseVariantIsRegisteredCollidesByMoniker(t *testing.T) {
	hygiene.Isolate(t)
	answers := append(npmFree("foo-bar", "foo.bar"),
		get("https://registry.npmjs.org/foo_bar", 200, `{}`),
		get("https://registry.npmjs.org/foobar", 404, ""),
		get("https://registry.npmjs.org/-/v1/search?text=foo-bar&size=20", 200, npmSearchEmpty),
	)
	fake := testsupport.NewFakeHTTP(t, answers...)
	withRegistry(t, fake, func(c Client, waits *[]time.Duration) error {
		r, err := c.CheckName(Npm, "foo-bar", 200*time.Millisecond)
		if err != nil || r.Status != StatusTaken || r.Reason != "moniker" {
			t.Fatalf("%+v %v", r, err)
		}
		if !slices.Equal(r.Conflicts, []Conflict{{Name: "foo_bar", Rule: RuleNpmMoniker}}) || !strings.Contains(r.Note, "'foo_bar'") {
			t.Errorf("%+v", r)
		}
		if !slices.Equal(r.Checked, []string{"npm", "variants", "moniker similarity"}) {
			t.Errorf("checked %v", r.Checked)
		}
		// Three variants, so two waits between them.
		if len(*waits) != 2 {
			t.Errorf("waits %v", *waits)
		}
		return nil
	})
}

func TestAnNpmSearchConflictMakesTheNameTaken(t *testing.T) {
	hygiene.Isolate(t)
	answers := append(npmFree("ab", "a-b", "a.b", "a_b"),
		get("https://registry.npmjs.org/-/v1/search?text=ab&size=20", 200, `{"objects":[{"package":{"name":"A-B-"}},{"package":{"name":"abc"}},{"package":{"name":"ab"}}]}`),
	)
	fake := testsupport.NewFakeHTTP(t, answers...)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		r, _ := c.CheckName(Npm, "ab", 0)
		if r.Status != StatusTaken || r.Reason != "moniker" || !slices.Equal(r.Conflicts, []Conflict{{Name: "A-B-", Rule: RuleNpmMoniker}}) {
			t.Errorf("%+v", r)
		}
		return nil
	})
}

// A variant the registry cannot answer about makes the verdict an error: an
// unasked variant might be the collision.
func TestAnUnansweredVariantIsAnError(t *testing.T) {
	hygiene.Isolate(t)
	answers := append(npmFree("ab", "a-b", "a.b"), get("https://registry.npmjs.org/a_b", 503, ""))
	fake := testsupport.NewFakeHTTP(t, answers...)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		r, _ := c.CheckName(Npm, "ab", 0)
		if r.Status != StatusError || !strings.Contains(r.Error, "a_b") || r.ExitCode() != 2 {
			t.Errorf("%+v", r)
		}
		return nil
	})
}

func TestAnNpmSearchFailureIsAnErrorUnlessACollisionIsKnown(t *testing.T) {
	hygiene.Isolate(t)
	answers := append(npmFree("ab", "a-b", "a.b", "a_b"), get("https://registry.npmjs.org/-/v1/search?text=ab&size=20", 500, ""))
	fake := testsupport.NewFakeHTTP(t, answers...)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		r, _ := c.CheckName(Npm, "ab", 0)
		if r.Status != StatusError || !strings.Contains(r.Error, "moniker check failed") {
			t.Errorf("%+v", r)
		}
		return nil
	})
}

func TestAPypiNameMatchingTheStandardLibraryIsTakenOffline(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		r, _ := c.CheckName(Pypi, "Json", 0)
		if r.Status != StatusTaken || r.Reason != "stdlib" || !slices.Equal(r.Conflicts, []Conflict{{Name: "json", Rule: RuleStdlib}}) || !strings.Contains(r.Note, "'json'") {
			t.Errorf("%+v", r)
		}
		return nil
	})
	if len(fake.URLs()) != 0 {
		t.Fatalf("a registry was asked: %v", fake.URLs())
	}
}

func TestAPypiNameRegisteredOnTheSimpleIndexIsTaken(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t, get("https://pypi.org/simple/my-gadget/", 200, "<html></html>"))
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		r, _ := c.CheckName(Pypi, "My_Gadget", 0)
		if r.Status != StatusTaken || r.Reason != "registered" || !slices.Equal(r.Checked, []string{"PyPI", "stdlib"}) {
			t.Errorf("%+v", r)
		}
		return nil
	})
}

// A name free under every separator spelling is still taken when a visually
// similar name (l/1, o/0) is registered.
func TestAVisuallySimilarPypiNameIsAnUltranormCollision(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t,
		get("https://pypi.org/simple/lo/", 404, ""),
		get("https://pypi.org/simple/l-o/", 404, ""),
		get("https://pypi.org/simple/l0/", 404, ""),
		get("https://pypi.org/simple/1o/", 200, ""),
	)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		r, _ := c.CheckName(Pypi, "lo", 0)
		if r.Status != StatusTaken || r.Reason != "ultranorm" || !slices.Equal(r.UltranormConflicts, []string{"1o"}) {
			t.Errorf("%+v", r)
		}
		if !slices.Contains(r.Checked, "ultranormalization") || !slices.Equal(r.Conflicts, []Conflict{{Name: "1o", Rule: RulePypiUltranorm}}) {
			t.Errorf("%+v", r)
		}
		return nil
	})
}

func TestAnAvailablePypiName(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t,
		get("https://pypi.org/simple/zx/", 404, ""),
		get("https://pypi.org/simple/z-x/", 404, ""),
	)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		r, _ := c.CheckName(Pypi, "zx", 0)
		if r.Status != StatusAvailable || r.ExitCode() != 0 || !slices.Equal(r.Checked, []string{"PyPI", "stdlib", "variants", "ultranormalization"}) {
			t.Errorf("%+v", r)
		}
		return nil
	})
}

// The go check asks nothing and a batch of go names never waits.
func TestGoNamesAreJudgedOfflineWithoutWaiting(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t)
	withRegistry(t, fake, func(c Client, waits *[]time.Duration) error {
		results, err := c.CheckNames(Go, []string{"testsandbox", "testing", "go-x"}, 200*time.Millisecond)
		if err != nil {
			t.Fatal(err)
		}
		var statuses []string
		for _, r := range results {
			statuses = append(statuses, r.Status)
		}
		if !slices.Equal(statuses, []string{"available", "taken", "invalid"}) || len(*waits) != 0 {
			t.Errorf("%v, waits %v", statuses, *waits)
		}
		if !slices.Equal(results[1].Conflicts, []Conflict{{Name: "testing", Rule: RuleGoStdlib}}) {
			t.Errorf("%+v", results[1])
		}
		return nil
	})
	if len(fake.URLs()) != 0 {
		t.Fatalf("a registry was asked: %v", fake.URLs())
	}
}

func TestExitCodes(t *testing.T) {
	hygiene.Isolate(t)
	for status, code := range map[string]int{StatusAvailable: 0, StatusTaken: 1, StatusInvalid: 1, StatusDiscouraged: 1, StatusError: 2} {
		if got := (NameResult{Status: status}).ExitCode(); got != code {
			t.Errorf("%s: %d, want %d", status, got, code)
		}
	}
}

func TestConflictsAreSortedByNameThenRule(t *testing.T) {
	hygiene.Isolate(t)
	var r NameResult
	r.addConflicts([]string{"zeta", "alpha"}, RulePypiSeparator)
	r.addConflicts([]string{"alpha"}, RulePypiUltranorm)
	want := []Conflict{{"alpha", RulePypiSeparator}, {"alpha", RulePypiUltranorm}, {"zeta", RulePypiSeparator}}
	if !slices.Equal(r.Conflicts, want) {
		t.Fatalf("%v", r.Conflicts)
	}
}

func TestAnUnknownTargetIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	fake := testsupport.NewFakeHTTP(t)
	withRegistry(t, fake, func(c Client, _ *[]time.Duration) error {
		if _, err := c.CheckName("github", "x", 0); err == nil || !strings.Contains(err.Error(), "npm, pypi, and go") {
			t.Errorf("%v", err)
		}
		return nil
	})
}
